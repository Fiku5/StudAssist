import pytest
from unittest.mock import AsyncMock, MagicMock
from recorder.schedule_manager import (
    Subject,
    ScheduleManager,
    parse_telegram_topic_url,
)
from bot.keyboards import (
    get_subjects_management_keyboard,
    get_subject_topic_edit_keyboard,
)
from publisher.telegram_pub import TelegramPublisher
from ai.schemas import ActionItemsDigest


def test_parse_telegram_topic_url():
    # 1. Standard private supergroup topic URL
    chat_id, thread_id = parse_telegram_topic_url("https://t.me/c/2443546271/3538")
    assert chat_id == -1002443546271
    assert thread_id == 3538

    # 2. Another internal ID
    chat_id, thread_id = parse_telegram_topic_url("https://t.me/c/123456789/42")
    assert chat_id == -100123456789
    assert thread_id == 42

    # 3. Public group username link
    chat_id, thread_id = parse_telegram_topic_url("https://t.me/student_group/999")
    assert chat_id is None
    assert thread_id == 999

    # 4. Pure integer
    chat_id, thread_id = parse_telegram_topic_url("3538")
    assert chat_id is None
    assert thread_id == 3538

    # 5. Hashtag formatted integer
    chat_id, thread_id = parse_telegram_topic_url("#3538")
    assert chat_id is None
    assert thread_id == 3538

    # 6. Invalid strings
    assert parse_telegram_topic_url("") == (None, None)
    assert parse_telegram_topic_url("random text") == (None, None)
    assert parse_telegram_topic_url("https://t.me/no_topic_link") == (None, None)


def test_subject_model_and_schedule_manager(tmp_path):
    data_file = tmp_path / "schedule_test.json"
    mgr = ScheduleManager(data_file=data_file)

    subj = mgr.add_subject("Архитектура ПО", short_name="Архитектура")
    assert subj.telegram_thread_id is None

    # Update thread ID
    success = mgr.update_subject_thread_id(subj.id, 3538)
    assert success is True

    # Reload from disk to verify persistence
    mgr2 = ScheduleManager(data_file=data_file)
    reloaded = mgr2.get_subject(subj.id)
    assert reloaded is not None
    assert reloaded.telegram_thread_id == 3538

    # Clear thread ID
    mgr2.update_subject_thread_id(subj.id, None)
    mgr3 = ScheduleManager(data_file=data_file)
    assert mgr3.get_subject(subj.id).telegram_thread_id is None


def test_keyboards_topic_rendering():
    s1 = Subject(id="s1", title="Архитектура", short_name="АПО", telegram_thread_id=3538)
    s2 = Subject(id="s2", title="Базы данных", short_name="БД", telegram_thread_id=None)

    kb = get_subjects_management_keyboard([s1, s2])
    # Check that s1 has "💬 #3538"
    s1_row = kb.inline_keyboard[0]
    assert any("💬 #3538" in btn.text for btn in s1_row)

    # Check that s2 has "🔗 Привязать тему"
    s2_row = kb.inline_keyboard[1]
    assert any("🔗 Привязать тему" in btn.text for btn in s2_row)

    # Topic edit keyboards
    edit_kb_bound = get_subject_topic_edit_keyboard("s1", has_topic=True)
    assert any("🗑 Отвязать тему" in btn.text for row in edit_kb_bound.inline_keyboard for btn in row)

    edit_kb_unbound = get_subject_topic_edit_keyboard("s2", has_topic=False)
    assert not any("🗑 Отвязать тему" in btn.text for row in edit_kb_unbound.inline_keyboard for btn in row)


@pytest.mark.anyio
async def test_telegram_publisher_topic_dispatch():
    pub = TelegramPublisher(token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz", channel_id="-1002443546271")
    pub.simulation_mode = False
    pub.bot = MagicMock()
    pub.bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))

    digest = ActionItemsDigest(
        subject="Архитектура ПО",
        topic="Введение в микросервисы",
        executive_summary="Краткий обзор лекции",
        action_items=[],
        key_technologies_and_terms=["Docker", "gRPC"],
    )

    # 1. Publish to specific topic without keyboard
    msg_id = await pub.publish_to_channel(
        digest=digest,
        full_article_url="https://github.com/org/repo/tree/main/lecture",
        chat_id="-1002443546271",
        message_thread_id=3538,
        with_keyboard=False,
    )
    assert msg_id == 12345
    call_kwargs = pub.bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == "-1002443546271"
    assert call_kwargs["message_thread_id"] == 3538
    assert "reply_markup" not in call_kwargs or call_kwargs.get("reply_markup") is None


@pytest.mark.anyio
async def test_group_topic_posting_logic(tmp_path):
    mgr = ScheduleManager(data_file=tmp_path / "sched.json")
    s_bound = mgr.add_subject("Архитектура ПО", short_name="АПО")
    mgr.update_subject_thread_id(s_bound.id, 3538)

    s_unbound = mgr.add_subject("Машинное обучение", short_name="МО")

    bot_mock = MagicMock()
    bot_mock.send_message = AsyncMock()

    # Simulation of handle_session_run_ai delivery logic
    async def dispatch_digest(subj_slug: str, digest_text: str):
        subj = mgr.get_subject(subj_slug)
        target_group_id = "-1002443546271"
        if subj and subj.telegram_thread_id and target_group_id:
            await bot_mock.send_message(
                chat_id=target_group_id,
                message_thread_id=subj.telegram_thread_id,
                text=digest_text,
                reply_markup=None,
            )

    # 1. Unbound subject: should NOT send to group
    await dispatch_digest(s_unbound.id, "digest text")
    assert bot_mock.send_message.call_count == 0

    # 2. Bound subject: sends to group topic without buttons
    await dispatch_digest(s_bound.id, "digest text")
    assert bot_mock.send_message.call_count == 1
    call_args = bot_mock.send_message.call_args.kwargs
    assert call_args["chat_id"] == "-1002443546271"
    assert call_args["message_thread_id"] == 3538
    assert call_args["reply_markup"] is None
    assert call_args["text"] == "digest text"

