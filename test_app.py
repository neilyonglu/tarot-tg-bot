"""Run offline regression checks with: uv run python test_app.py."""

import asyncio
import contextlib
import io
import os
import runpy
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
from telegram.error import BadRequest, NetworkError


def message(message_id=10):
    return SimpleNamespace(
        chat_id=1, message_id=message_id, text="工作方向",
        reply_text=AsyncMock(return_value=SimpleNamespace(chat_id=1, message_id=20)),
        reply_photo=AsyncMock(),
    )


async def main():
    with patch.object(app, "TELEGRAM_TOKEN", ""), patch.object(app, "GEMINI_API_KEY", ""):
        try:
            app.validate_config()
        except RuntimeError as error:
            assert "TELEGRAM_TOKEN" in str(error) and "GEMINI_API_KEY" in str(error)
        else:
            raise AssertionError("Missing credentials must fail validation.")
    with patch.object(app, "TELEGRAM_TOKEN", "123:TEST"), patch.object(app, "GEMINI_API_KEY", "test-key"):
        with patch.dict(os.environ, {"PORT": "10000"}):
            app.validate_config()
        with patch.dict(os.environ, {"PORT": "65536"}):
            try:
                app.validate_config()
            except RuntimeError as error:
                assert "PORT" in str(error)
            else:
                raise AssertionError("Invalid ports must fail validation.")

    instant = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    with patch.object(app, "datetime") as clock:
        clock.now.return_value = instant.astimezone(app.TAIWAN_TIME)
        user = {"last_usage_date": "2026-10-08", "usage_count": 7}
        assert app.get_remaining_uses(user) == 7
        assert user["last_usage_date"] == "2026-10-09"
        clock.now.assert_called_with(app.TAIWAN_TIME)

    context = SimpleNamespace(user_data={}, args=["未設定密碼"])
    update = SimpleNamespace(message=message())
    with patch.object(app, "SECRET_PASSWORD", ""):
        await app.handle_pwd(update, context)
        assert not context.user_data.get("is_unlocked")
        update.message.reply_text.assert_awaited_once_with("VIP 解鎖目前未開放。")
        await app.handle_text_input(update, context)
    assert app.get_remaining_uses(context.user_data) == 7
    reading_id = context.user_data["reading_id"]

    query = SimpleNamespace(
        data=f"draw_1:{reading_id}", message=message(),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    update.callback_query = query
    with patch.object(app.asyncio, "sleep", new_callable=AsyncMock):
        await app.button_handler(update, context)
        await app.button_handler(update, context)
    assert query.message.reply_photo.await_count == 1
    assert app.get_remaining_uses(context.user_data) == 6
    assert query.answer.await_args.kwargs["show_alert"]

    mode_query = SimpleNamespace(data=f"mode_builtin:{reading_id}", message=message(20), answer=AsyncMock())
    mode_query.message.reply_text.return_value = SimpleNamespace(chat_id=1, message_id=21)
    update.callback_query = mode_query
    with patch.object(app, "get_gemini_response", new_callable=AsyncMock, side_effect=RuntimeError("429")):
        await app.button_handler(update, context)
    assert context.user_data["mode_pending"] == (1, 21)
    assert app.get_remaining_uses(context.user_data) == 6
    with patch.object(app, "get_gemini_response", new_callable=AsyncMock) as generate:
        await app.button_handler(update, context)
        generate.assert_not_awaited()

    update.callback_query = SimpleNamespace(data=f"mode_copy:{reading_id}", message=message(21), answer=AsyncMock())
    await app.button_handler(update, context)
    assert context.user_data["is_follow_up_mode"]
    assert context.user_data["mode_pending"] is None
    sent = update.callback_query.message.reply_text.await_args_list
    assert any("工作方向" in call.args[0] for call in sent)
    before = update.callback_query.message.reply_text.await_count
    await app.button_handler(update, context)
    assert update.callback_query.message.reply_text.await_count == before

    app._reset_reading(context.user_data)
    await app.button_handler(update, context)
    assert update.callback_query.answer.await_args.kwargs["show_alert"]
    assert app.get_remaining_uses(context.user_data) == 6
    await app.handle_status(update, context)
    assert "6 / 7" in update.message.reply_text.await_args.args[0]
    context.user_data["is_unlocked"] = True
    await app.handle_status(update, context)
    assert "無限制" in update.message.reply_text.await_args.args[0]
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await app.post_init(SimpleNamespace(bot=bot))
    assert "status" in [command.command for command in bot.set_my_commands.await_args.args[0]]

    reply = message()
    text = "😀<&>" * 2500
    await app.safe_reply_with_html(reply, f"<b>{app.html.escape(text)}</b>", "keyboard")
    calls = reply.reply_text.await_args_list
    assert "".join(call.args[0] for call in calls) == text
    assert all(len(call.args[0].encode("utf-16-le")) <= 8000 for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] == "keyboard"
    reply.reply_text.reset_mock()
    reply.reply_text.side_effect = [BadRequest("Can't parse entities"), None]
    await app.safe_reply_with_html(reply, "<b>broken &amp; text", "keyboard")
    assert reply.reply_text.await_args.args[0] == "broken & text"
    reply.reply_text.reset_mock()
    reply.reply_text.side_effect = NetworkError("offline")
    try:
        await app.safe_reply_with_html(reply, "text")
    except NetworkError:
        assert reply.reply_text.await_count == 1
    else:
        raise AssertionError("Network errors must not resend the message.")

    generate = AsyncMock(side_effect=[RuntimeError("429"), RuntimeError("503"), SimpleNamespace(text="ok")])
    fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    with patch.object(app, "client", fake_client), patch.object(app.asyncio, "sleep", new_callable=AsyncMock):
        assert await app.get_gemini_response("test") == "ok"
        assert [call.kwargs["model"] for call in generate.await_args_list] == [app.GEMINI_MODEL_PRIMARY] * 2 + [app.GEMINI_MODEL_FALLBACK]
        failure = RuntimeError("401")
        generate.reset_mock(side_effect=True)
        generate.side_effect = failure
        try:
            await app.get_gemini_response("test")
        except RuntimeError as error:
            assert error is failure and generate.await_count == 1
        else:
            raise AssertionError("Non-retryable errors must propagate.")

    assert len(app.TAROT_DATA) == 78
    original_cwd = os.getcwd()
    with TemporaryDirectory() as directory:
        try:
            os.chdir(directory)
            assert len(runpy.run_path(str(app.BASE_DIR / "app.py"))["TAROT_DATA"]) == 78
            photo = app.get_card_image(next(iter(app.TAROT_DATA.values())), True)
            with app.Image.open(photo) as image:
                assert image.format == "JPEG"
            photo.close()
            with patch("requests.get") as download:
                runpy.run_path(str(app.BASE_DIR / "download_cards.py"))
                download.assert_not_called()
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(main())
    print("PASS: offline regression checks.")
