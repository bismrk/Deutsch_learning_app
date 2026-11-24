"""Unit tests for the training FSM flow, HTML formatters, keyboards, and error handling."""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote_plus

import httpx
import pytest
import respx
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    Sticker,
    Update,
    User,
    Voice,
)

from bot_app.api_client import (
    RATE_LIMIT_MESSAGE_UA,
    TIMEOUT_ERROR_MESSAGE_UA,
    UNAVAILABLE_ERROR_MESSAGE_UA,
    APIUnavailableError,
    BackendAPIClient,
    BadRequestError,
    ConflictError,
    NotFoundError,
    RateLimitError,
)
from bot_app.config import BotSettings
from bot_app.formatters import (
    EXERCISE_TYPE_LABELS_UA,
    HINT_ANTICHEAT_NOTE_UA,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    build_youtube_search_url,
    format_evaluation_message,
    format_exercise_message,
    format_hint_message,
    format_summary_message,
)
from bot_app.handlers.training import (
    EMPTY_ANSWER_MESSAGE_UA,
    EMPTY_DICTIONARY_MESSAGE_UA,
    NON_TEXT_ANSWER_MESSAGE_UA,
    STALE_SESSION_MESSAGE_UA,
    TEMPORARY_RETRY_HINT_UA,
    TOPIC_CANCELLED_MESSAGE_UA,
    TRAINING_MODE_PROMPT_UA,
    TrainingStates,
    handle_choose_topic_mode,
    handle_exercise_answer,
    handle_non_text_answer,
    handle_start_mix_mode,
    handle_topic_cancel,
    handle_topic_level_switch,
    handle_topic_selected,
    handle_topic_text_input,
    handle_training_abandon,
    handle_training_hint,
    handle_training_menu,
)
from bot_app.keyboards import (
    GRAMMAR_TOPICS,
    MENU_BTN_TRAINING,
    TRAIN_ABANDON_CALLBACK_PREFIX,
    TRAIN_HINT_CALLBACK_PREFIX,
    TRAIN_MODE_MIX_CALLBACK,
    TRAIN_MODE_TOPIC_CALLBACK,
    TRAIN_TOPIC_CALLBACK_PREFIX,
    TRAIN_TOPIC_CANCEL_CALLBACK,
    TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX,
    build_exercise_keyboard,
    build_topic_keyboard,
    build_training_mode_keyboard,
    resolve_grammar_topic,
)
from bot_app.main import create_dispatcher
from bot_app.schemas import (
    ActiveSessionResponse,
    ExerciseError,
    ExerciseEvaluation,
    ExercisePublic,
    SessionAbandonResponse,
    SessionAnswerResponse,
    SessionHintResponse,
    SessionStartResponse,
    SessionSummary,
    UserProfile,
    WordRating,
)

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "test-internal-api-key-14"
BOT_ID = 777000
USER_ID = 123456789
CHAT_ID = 123456789


class _StrictHTMLValidator(HTMLParser):
    """Minimal strict tag-balance validator for Telegram HTML output."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"Unexpected closing tag </{tag}> with stack={self.stack}")
        else:
            self.stack.pop()

    def assert_valid(self, html_text: str) -> None:
        self.feed(html_text)
        self.close()
        assert self.errors == [], f"HTML errors: {self.errors}"
        assert self.stack == [], f"Unclosed HTML tags: {self.stack}"


def _make_settings(miniapp_url: str | None = "https://deutsch.example.com") -> BotSettings:
    return BotSettings(
        _env_file=None,
        bot_token="123456:TEST-TOKEN",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url=miniapp_url,
    )


def _make_fsm() -> tuple[MemoryStorage, FSMContext]:
    storage = MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    return storage, FSMContext(storage=storage, key=key)


def _make_message(text: str | None = None, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _make_callback(data: str, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
    )


def _make_api_mock() -> AsyncMock:
    return AsyncMock(spec=BackendAPIClient)


def _make_profile(level: str = "A2") -> UserProfile:
    return UserProfile(
        telegram_id=USER_ID,
        level=level,  # type: ignore[arg-type]
        exercises_per_set=5,
        theme="system",
        sheets_url=None,
        reminder_hour=19,
        timezone="Europe/Kyiv",
        daily_llm_calls=0,
        created_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )


def _make_exercise(
    *,
    exercise_type: str = "translation",
    level: str = "B1",
    grammar_topic: str = "Nebensätze mit weil",
    instruction_ua: str = "Переклади речення німецькою:",
    task_text: str = "Я запізнився, тому що потяг мав затримку.",
) -> ExercisePublic:
    return ExercisePublic(
        exercise_type=exercise_type,  # type: ignore[arg-type]
        level=level,  # type: ignore[arg-type]
        grammar_topic=grammar_topic,
        target_word_ids=[14, 29],
        instruction_ua=instruction_ua,
        task_text=task_text,
    )


def _make_start_response(
    *,
    session_id: int = 42,
    mode: str = "mix",
    target_topic: str | None = None,
    current_number: int = 1,
    total_exercises: int = 5,
    exercise: ExercisePublic | None = None,
) -> SessionStartResponse:
    return SessionStartResponse(
        session_id=session_id,
        mode=mode,  # type: ignore[arg-type]
        target_topic=target_topic,
        total_exercises=total_exercises,
        completed_exercises=current_number - 1,
        current_exercise_number=current_number,
        status="active",
        created_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        attempt_id=101,
        used_hint=False,
        exercise=exercise or _make_exercise(),
    )


def _make_active_response(
    *,
    session_id: int = 42,
    current_number: int = 3,
    total_exercises: int = 5,
    used_hint: bool = False,
    hint_ua: str | None = None,
) -> ActiveSessionResponse:
    return ActiveSessionResponse(
        session_id=session_id,
        mode="mix",
        target_topic=None,
        total_exercises=total_exercises,
        completed_exercises=current_number - 1,
        current_exercise_number=current_number,
        status="active",
        created_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        attempt_id=103,
        used_hint=used_hint,
        exercise=_make_exercise(exercise_type="fill_blank", task_text="Ich gehe ___ Bahnhof."),
        hint_ua=hint_ua,
    )


def _make_evaluation(
    *,
    is_correct: bool = False,
    grammar_score: int = 65,
    corrected_answer: str = "Ich bin zu spät gekommen, weil der Zug Verspätung hatte.",
    errors: list[ExerciseError] | None = None,
    youtube_topic: str = "Порядок слів у реченні з weil німецька B1",
    summary_ua: str = "Лексику згадав точно, але підвів порядок слів.",
) -> ExerciseEvaluation:
    default_errors = (
        []
        if is_correct
        else [
            ExerciseError(
                wrong_part="weil der Zug hatte Verspätung",
                correct_part="weil der Zug Verspätung hatte",
                explanation_ua="Після сполучника weil дієслово стоїть у кінці.",
            ),
        ]
    )
    return ExerciseEvaluation(
        is_correct=is_correct,
        grammar_score=grammar_score,
        corrected_answer=corrected_answer,
        word_ratings=[
            WordRating(
                word_id=14,
                word_de="die Verspätung",
                fsrs_rating=4 if is_correct else 3,
                note_ua="Добре вжито слово",
            ),
        ],
        errors=default_errors if errors is None else errors,
        youtube_topic=youtube_topic,
        summary_ua=summary_ua,
    )


# ---------------------------------------------------------------------------
# Formatter & keyboard tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exercise_type", list(EXERCISE_TYPE_LABELS_UA.keys()))
def test_format_exercise_message_renders_progress_and_escapes_html(exercise_type: str) -> None:
    exercise = _make_exercise(
        exercise_type=exercise_type,
        level="B1",
        grammar_topic="Nebensätze <weil> & 'dass'",
        instruction_ua="Використай <артикль> & сполучник:",
        task_text="A < B && B > C",
    )

    rendered = format_exercise_message(
        exercise,
        current_number=2,
        total_exercises=5,
        resumed=True,
        hint_ua="Пам'ятай: <verb> & кінець речення",
    )

    assert "Вправа 2 із 5" in rendered
    assert EXERCISE_TYPE_LABELS_UA[exercise_type] in rendered
    assert "B1" in rendered
    assert "Продовжуємо активне тренування" in rendered
    assert "&lt;weil&gt; &amp;" in rendered
    assert "&lt;артикль&gt; &amp;" in rendered
    assert "A &lt; B &amp;&amp; B &gt; C" in rendered
    assert "&lt;verb&gt; &amp;" in rendered
    assert "<weil>" not in rendered
    assert "<verb>" not in rendered
    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(rendered)


def test_format_hint_message_escapes_html_and_mentions_fsrs_cap() -> None:
    rendered = format_hint_message("Порядок слів: <Subjekt> + ... & <Verb> в кінці.")

    assert "&lt;Subjekt&gt;" in rendered
    assert "&amp;" in rendered
    assert "&lt;Verb&gt;" in rendered
    assert HINT_ANTICHEAT_NOTE_UA in rendered
    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(rendered)


def test_format_evaluation_omits_errors_section_when_correct_and_empty_errors() -> None:
    evaluation = _make_evaluation(
        is_correct=True,
        grammar_score=100,
        corrected_answer="Ich komme heute <pünktlich> & sicher.",
        errors=[],
        youtube_topic="Perfekt mit haben & sein <A2>",
        summary_ua="Бездоганне речення!",
    )

    rendered = format_evaluation_message(evaluation)

    assert "✅" in rendered
    assert "100/100" in rendered
    assert "Бездоганне речення!" in rendered
    assert "Ich komme heute &lt;pünktlich&gt; &amp; sicher." in rendered
    assert "Розбір помилок" not in rendered
    expected_url = build_youtube_search_url("Perfekt mit haben & sein <A2>")
    assert quote_plus("Perfekt mit haben & sein <A2>") in expected_url
    assert f'href="{expected_url}"' in rendered
    assert "Perfekt mit haben &amp; sein &lt;A2&gt;" in rendered
    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(rendered)


def test_format_evaluation_renders_errors_with_html_escape_and_youtube_link() -> None:
    evaluation = _make_evaluation(
        is_correct=False,
        grammar_score=55,
        corrected_answer="Ich gehe in die Schule.",
        errors=[
            ExerciseError(
                wrong_part="in <der> & Schule",
                correct_part="in <die> & Schule",
                explanation_ua="Wohin? -> Akkusativ (<die> & nicht <der>).",
            ),
        ],
        youtube_topic="Wechselpräpositionen Akkusativ & Dativ",
        summary_ua="Є помилка у відмінку <Akkusativ> & прийменнику.",
    )

    rendered = format_evaluation_message(evaluation, used_hint=True)

    assert "❌" in rendered
    assert "55/100" in rendered
    assert "з підказкою" in rendered
    assert "Розбір помилок" in rendered
    assert "<s>in &lt;der&gt; &amp; Schule</s> → <b>in &lt;die&gt; &amp; Schule</b>" in rendered
    assert "Wohin? -&gt; Akkusativ (&lt;die&gt; &amp; nicht &lt;der&gt;)." in rendered
    assert "&lt;Akkusativ&gt; &amp;" in rendered
    assert "https://www.youtube.com/results?search_query=" in rendered
    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(rendered)


def test_format_evaluation_truncates_safely_within_telegram_limit() -> None:
    huge_errors = [
        ExerciseError(
            wrong_part=f"<wrong_{i}> & " + ("x" * 120),
            correct_part=f"<right_{i}> & " + ("y" * 120),
            explanation_ua=f"Пояснення #{i}: <rule> & " + ("z" * 240),
        )
        for i in range(25)
    ]
    evaluation = _make_evaluation(
        is_correct=False,
        grammar_score=30,
        corrected_answer="<corrected> & " + ("a" * 1500),
        errors=huge_errors,
        youtube_topic="Дуже довга тема & <тег> " + ("тема " * 80),
        summary_ua="<summary> & " + ("б" * 1500),
    )

    rendered = format_evaluation_message(evaluation)

    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert "https://www.youtube.com/results?search_query=" in rendered
    assert "<wrong_0>" not in rendered
    assert "<summary>" not in rendered
    _StrictHTMLValidator().assert_valid(rendered)

    # Oversized evaluation without errors must also stay <= 4096 with valid HTML.
    no_errors_huge = _make_evaluation(
        is_correct=True,
        grammar_score=100,
        corrected_answer="<ans> & " + ("a" * 3000),
        errors=[],
        youtube_topic="Тема & <B1>",
        summary_ua="<sum> & " + ("б" * 3000),
    )
    rendered_no_errors = format_evaluation_message(no_errors_huge)
    assert len(rendered_no_errors) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert "Розбір помилок" not in rendered_no_errors
    _StrictHTMLValidator().assert_valid(rendered_no_errors)

    # Oversized exercise payload must stay <= 4096 with valid HTML.
    huge_exercise = _make_exercise(
        instruction_ua="<inst> & " + ("і" * 2000),
        task_text="<task> & " + ("т" * 3500),
    )
    rendered_exercise = format_exercise_message(
        huge_exercise,
        current_number=1,
        total_exercises=5,
        resumed=True,
        hint_ua="<hint> & " + ("п" * 1500),
    )
    assert len(rendered_exercise) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(rendered_exercise)


def test_format_summary_with_and_without_weak_topics() -> None:
    with_weak = format_summary_message(
        SessionSummary(
            total_exercises=5,
            correct_count=3,
            avg_grammar_score=74.5,
            weak_topics=["Nebensätze mit <weil> & dass", "Dativ nach Präpositionen"],
        ),
    )
    assert "3 із 5" in with_weak
    assert "74.5/100" in with_weak
    assert "Nebensätze mit &lt;weil&gt; &amp; dass" in with_weak
    assert "https://www.youtube.com/results?search_query=" in with_weak
    assert len(with_weak) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(with_weak)

    without_weak = format_summary_message(
        SessionSummary(
            total_exercises=5,
            correct_count=5,
            avg_grammar_score=96.0,
            weak_topics=[],
        ),
    )
    assert "5 із 5" in without_weak
    assert "96/100" in without_weak
    assert "Слабких тем" in without_weak
    assert len(without_weak) <= TELEGRAM_MAX_MESSAGE_LENGTH
    _StrictHTMLValidator().assert_valid(without_weak)


def test_training_keyboards_have_valid_callback_data_within_64_bytes() -> None:
    mode_kb = build_training_mode_keyboard()
    mode_callbacks = [btn.callback_data for row in mode_kb.inline_keyboard for btn in row]
    assert mode_callbacks == [TRAIN_MODE_MIX_CALLBACK, TRAIN_MODE_TOPIC_CALLBACK]

    for level in ("A1", "A2", "B1"):
        topic_kb = build_topic_keyboard(level)  # type: ignore[arg-type]
        all_buttons = [btn for row in topic_kb.inline_keyboard for btn in row]
        assert any(btn.callback_data == TRAIN_TOPIC_CANCEL_CALLBACK for btn in all_buttons)
        for btn in all_buttons:
            assert btn.callback_data is not None
            assert len(btn.callback_data.encode("utf-8")) <= 64
        for idx, expected_topic in enumerate(GRAMMAR_TOPICS[level]):  # type: ignore[index]
            assert resolve_grammar_topic(level, str(idx)) == expected_topic

    exercise_kb = build_exercise_keyboard(42)
    ex_callbacks = [btn.callback_data for row in exercise_kb.inline_keyboard for btn in row]
    assert ex_callbacks == [
        f"{TRAIN_HINT_CALLBACK_PREFIX}42",
        f"{TRAIN_ABANDON_CALLBACK_PREFIX}42",
    ]


# ---------------------------------------------------------------------------
# Handler & FSM tests
# ---------------------------------------------------------------------------


async def test_training_menu_shows_mode_selection() -> None:
    _, state = _make_fsm()
    message = _make_message(MENU_BTN_TRAINING)

    await handle_training_menu(message=message, state=state)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == TRAINING_MODE_PROMPT_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)


async def test_start_mix_mode_sets_waiting_answer_and_sends_exercise() -> None:
    _, state = _make_fsm()
    api_client = _make_api_mock()
    api_client.start_session.return_value = _make_start_response(session_id=77, mode="mix")
    callback = _make_callback(TRAIN_MODE_MIX_CALLBACK)

    await handle_start_mix_mode(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    callback.answer.assert_awaited_once()
    api_client.start_session.assert_awaited_once_with(USER_ID, mode="mix", target_topic=None)
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 77

    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    assert "Вправа 1 із 5" in text
    assert callback.message.answer.await_args.kwargs["parse_mode"] == "HTML"
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert callbacks == [f"{TRAIN_HINT_CALLBACK_PREFIX}77", f"{TRAIN_ABANDON_CALLBACK_PREFIX}77"]


async def test_topic_mode_inline_selection_and_cancel_flow() -> None:
    _, state = _make_fsm()
    api_client = _make_api_mock()
    api_client.get_me.return_value = _make_profile(level="A2")
    settings = _make_settings()

    # Step 1: Enter topic selection
    choose_cb = _make_callback(TRAIN_MODE_TOPIC_CALLBACK)
    await handle_choose_topic_mode(callback=choose_cb, state=state, api_client=api_client)
    assert await state.get_state() == TrainingStates.choosing_topic.state
    choose_cb.answer.assert_awaited_once()
    choose_cb.message.answer.assert_awaited_once()
    assert "A2" in choose_cb.message.answer.await_args.args[0]

    # Step 2: Switch topic level to B1
    switch_cb = _make_callback(f"{TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX}B1")
    await handle_topic_level_switch(callback=switch_cb, state=state)
    assert await state.get_state() == TrainingStates.choosing_topic.state
    switch_cb.answer.assert_awaited_once()
    switch_cb.message.edit_text.assert_awaited_once()
    assert "B1" in switch_cb.message.edit_text.await_args.args[0]

    # Step 3: Cancel clears FSM and returns main menu
    cancel_cb = _make_callback(TRAIN_TOPIC_CANCEL_CALLBACK)
    await handle_topic_cancel(callback=cancel_cb, state=state, settings=settings)
    assert await state.get_state() is None
    cancel_cb.answer.assert_awaited_once()
    assert cancel_cb.message.answer.await_args.args[0] == TOPIC_CANCELLED_MESSAGE_UA
    assert isinstance(
        cancel_cb.message.answer.await_args.kwargs["reply_markup"],
        ReplyKeyboardMarkup,
    )

    # Step 4: Select an inline B1 topic directly -> starts topic session
    expected_topic = GRAMMAR_TOPICS["B1"][0]
    api_client.start_session.return_value = _make_start_response(
        session_id=88,
        mode="topic",
        target_topic=expected_topic,
        exercise=_make_exercise(grammar_topic=expected_topic),
    )
    select_cb = _make_callback(f"{TRAIN_TOPIC_CALLBACK_PREFIX}B1:0")
    await handle_topic_selected(
        callback=select_cb,
        state=state,
        api_client=api_client,
        settings=settings,
    )
    api_client.start_session.assert_awaited_once_with(
        USER_ID,
        mode="topic",
        target_topic=expected_topic,
    )
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 88


async def test_topic_mode_custom_text_input_starts_topic_session() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.choosing_topic)
    api_client = _make_api_mock()
    api_client.start_session.return_value = _make_start_response(
        session_id=91,
        mode="topic",
        target_topic="Konjunktiv II",
        exercise=_make_exercise(grammar_topic="Konjunktiv II"),
    )
    message = _make_message("  Konjunktiv II  ")

    await handle_topic_text_input(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.start_session.assert_awaited_once_with(
        USER_ID,
        mode="topic",
        target_topic="Konjunktiv II",
    )
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 91


async def test_empty_dictionary_returns_friendly_message_without_entering_waiting_answer() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.choosing_topic)
    api_client = _make_api_mock()
    api_client.start_session.side_effect = BadRequestError(
        "Ваш словник порожній. Додайте хоча б одне слово перед початком тренування.",
        status_code=400,
    )
    callback = _make_callback(TRAIN_MODE_MIX_CALLBACK)

    await handle_start_mix_mode(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() is None
    callback.message.answer.assert_awaited_once()
    reply = callback.message.answer.await_args.args[0]
    assert reply == EMPTY_DICTIONARY_MESSAGE_UA
    assert "3–5" in reply
    assert isinstance(
        callback.message.answer.await_args.kwargs["reply_markup"],
        ReplyKeyboardMarkup,
    )


async def test_conflict_409_recovers_active_session_after_bot_restart() -> None:
    # Fresh MemoryStorage simulates container restart with lost FSM state.
    _, state = _make_fsm()
    assert await state.get_state() is None

    api_client = _make_api_mock()
    api_client.start_session.side_effect = ConflictError(
        "У вас уже є активна тренувальна сесія.",
        status_code=409,
    )
    api_client.get_active_session.return_value = _make_active_response(
        session_id=55,
        current_number=3,
        total_exercises=5,
        used_hint=True,
        hint_ua="Зверни увагу на артикль після zu.",
    )
    callback = _make_callback(TRAIN_MODE_MIX_CALLBACK)

    await handle_start_mix_mode(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.get_active_session.assert_awaited_once_with(USER_ID)
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 55

    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    assert "Продовжуємо активне тренування" in text
    assert "Вправа 3 із 5" in text
    assert "Зверни увагу на артикль після zu." in text


async def test_hint_callback_shows_hint_and_keeps_waiting_answer() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42)

    api_client = _make_api_mock()
    api_client.request_hint.return_value = SessionHintResponse(
        hint_ua="Дієслово після <weil> ставимо в кінець.",
        used_hint=True,
    )
    callback = _make_callback(f"{TRAIN_HINT_CALLBACK_PREFIX}42")

    await handle_training_hint(callback=callback, state=state, api_client=api_client)

    api_client.request_hint.assert_awaited_once_with(USER_ID, 42)
    callback.answer.assert_awaited_once()
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 42

    callback.message.answer.assert_awaited_once()
    hint_text = callback.message.answer.await_args.args[0]
    assert "&lt;weil&gt;" in hint_text
    assert HINT_ANTICHEAT_NOTE_UA in hint_text


@pytest.mark.parametrize(
    "exc",
    [
        ConflictError("Підказка недоступна для завершеної сесії.", status_code=409),
        NotFoundError("Тренувальну сесію не знайдено.", status_code=404),
    ],
)
async def test_stale_hint_callback_handles_404_and_409_without_breaking_active_session(
    exc: ConflictError | NotFoundError,
) -> None:
    _, state = _make_fsm()
    # User is already in a newer active session #99 and clicks an old button for session #12.
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=99)

    api_client = _make_api_mock()
    api_client.request_hint.side_effect = exc
    callback = _make_callback(f"{TRAIN_HINT_CALLBACK_PREFIX}12")

    await handle_training_hint(callback=callback, state=state, api_client=api_client)

    callback.answer.assert_awaited_once()
    callback.message.answer.assert_awaited_once_with(STALE_SESSION_MESSAGE_UA)
    # Newer session #99 state must remain untouched.
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 99


async def test_abandon_callback_clears_fsm_and_restores_main_menu() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42)

    api_client = _make_api_mock()
    api_client.abandon_session.return_value = SessionAbandonResponse(
        session_id=42,
        status="abandoned",
        completed_exercises=2,
        total_exercises=5,
    )
    callback = _make_callback(f"{TRAIN_ABANDON_CALLBACK_PREFIX}42")

    await handle_training_abandon(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.abandon_session.assert_awaited_once_with(USER_ID, 42)
    callback.answer.assert_awaited_once()
    assert await state.get_state() is None
    assert await state.get_data() == {}

    callback.message.answer.assert_awaited_once()
    reply = callback.message.answer.await_args.args[0]
    assert "2 із 5" in reply
    assert isinstance(
        callback.message.answer.await_args.kwargs["reply_markup"],
        ReplyKeyboardMarkup,
    )


@pytest.mark.parametrize(
    "exc",
    [
        ConflictError("Сесія вже завершена.", status_code=409),
        NotFoundError("Сесію не знайдено.", status_code=404),
    ],
)
async def test_stale_abandon_callback_handles_404_and_409_gracefully(
    exc: ConflictError | NotFoundError,
) -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42)

    api_client = _make_api_mock()
    api_client.abandon_session.side_effect = exc
    callback = _make_callback(f"{TRAIN_ABANDON_CALLBACK_PREFIX}42")

    await handle_training_abandon(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    callback.answer.assert_awaited_once()
    assert await state.get_state() is None
    callback.message.answer.assert_awaited_once()
    assert callback.message.answer.await_args.args[0] == STALE_SESSION_MESSAGE_UA
    assert isinstance(
        callback.message.answer.await_args.kwargs["reply_markup"],
        ReplyKeyboardMarkup,
    )


async def test_answer_active_sends_evaluation_and_next_exercise() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42, current_exercise_number=1, total_exercises=5)

    next_ex = _make_exercise(
        exercise_type="find_error",
        grammar_topic="Dativ nach Präpositionen",
        task_text="Ich fahre mit der Zug.",
    )
    api_client = _make_api_mock()
    api_client.submit_answer.return_value = SessionAnswerResponse(
        session_id=42,
        attempt_id=101,
        status="active",
        completed_exercises=1,
        total_exercises=5,
        used_hint=False,
        evaluation=_make_evaluation(is_correct=False, grammar_score=65),
        next_attempt_id=102,
        next_exercise=next_ex,
        summary=None,
    )
    message = _make_message("Ich bin zu spät gekommen, weil der Zug hatte Verspätung.")

    await handle_exercise_answer(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.submit_answer.assert_awaited_once_with(
        USER_ID,
        42,
        "Ich bin zu spät gekommen, weil der Zug hatte Verspätung.",
    )
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["current_exercise_number"] == 2
    assert message.answer.await_count == 2

    eval_call = message.answer.await_args_list[0]
    assert "❌" in eval_call.args[0]
    assert "65/100" in eval_call.args[0]
    assert "Розбір помилок" in eval_call.args[0]
    assert eval_call.kwargs["parse_mode"] == "HTML"

    next_call = message.answer.await_args_list[1]
    assert "Вправа 2 із 5" in next_call.args[0]
    assert "Ich fahre mit der Zug." in next_call.args[0]
    assert isinstance(next_call.kwargs["reply_markup"], InlineKeyboardMarkup)


async def test_answer_completed_clears_fsm_and_sends_summary_with_main_menu() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42, current_exercise_number=5, total_exercises=5)

    api_client = _make_api_mock()
    api_client.submit_answer.return_value = SessionAnswerResponse(
        session_id=42,
        attempt_id=105,
        status="completed",
        completed_exercises=5,
        total_exercises=5,
        used_hint=False,
        evaluation=_make_evaluation(is_correct=True, grammar_score=95, errors=[]),
        next_attempt_id=None,
        next_exercise=None,
        summary=SessionSummary(
            total_exercises=5,
            correct_count=4,
            avg_grammar_score=86.5,
            weak_topics=["Nebensätze mit weil"],
        ),
    )
    message = _make_message("Ich fahre mit dem Zug.")

    await handle_exercise_answer(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert message.answer.await_count == 2

    eval_text = message.answer.await_args_list[0].args[0]
    assert "✅" in eval_text
    assert "Розбір помилок" not in eval_text

    summary_call = message.answer.await_args_list[1]
    assert "4 із 5" in summary_call.args[0]
    assert "86.5/100" in summary_call.args[0]
    assert "Nebensätze mit weil" in summary_call.args[0]
    assert isinstance(summary_call.kwargs["reply_markup"], ReplyKeyboardMarkup)


async def test_empty_and_non_text_answers_never_call_submit_answer() -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42)
    api_client = _make_api_mock()

    whitespace_msg = _make_message("    ")
    await handle_exercise_answer(
        message=whitespace_msg,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )
    whitespace_msg.answer.assert_awaited_once_with(EMPTY_ANSWER_MESSAGE_UA)
    api_client.submit_answer.assert_not_awaited()
    assert await state.get_state() == TrainingStates.waiting_answer.state

    non_text_msg = _make_message(text=None)
    await handle_non_text_answer(message=non_text_msg)
    non_text_msg.answer.assert_awaited_once_with(NON_TEXT_ANSWER_MESSAGE_UA)
    api_client.submit_answer.assert_not_awaited()
    assert await state.get_state() == TrainingStates.waiting_answer.state


@pytest.mark.parametrize("media_kind", ["sticker", "photo", "voice"])
async def test_dispatcher_routes_non_text_media_in_waiting_answer_without_api_call(
    media_kind: str,
) -> None:
    api_client = _make_api_mock()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
    await dispatcher.storage.set_data(key, {"session_id": 42})

    bot = MagicMock()
    bot.id = BOT_ID
    bot.return_value = AsyncMock()
    sent_payloads: list[object] = []

    async def _capture_bot_call(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture_bot_call

    media_kwargs: dict[str, object] = {}
    if media_kind == "sticker":
        media_kwargs["sticker"] = Sticker(
            file_id="st1",
            file_unique_id="su1",
            type="regular",
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
        )
    elif media_kind == "photo":
        media_kwargs["photo"] = [
            PhotoSize(file_id="ph1", file_unique_id="pu1", width=100, height=100),
        ]
    else:
        media_kwargs["voice"] = Voice(file_id="vo1", file_unique_id="vu1", duration=3)

    msg = Message(
        message_id=10,
        date=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        **media_kwargs,  # type: ignore[arg-type]
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=msg))

    api_client.submit_answer.assert_not_awaited()
    assert await dispatcher.storage.get_state(key) == TrainingStates.waiting_answer.state
    assert len(sent_payloads) == 1
    assert getattr(sent_payloads[0], "text", "") == NON_TEXT_ANSWER_MESSAGE_UA


@respx.mock
async def test_start_command_in_waiting_answer_is_not_submitted_as_answer() -> None:
    profile_route = respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "telegram_id": USER_ID,
                "level": "A2",
                "exercises_per_set": 5,
                "theme": "system",
                "sheets_url": None,
                "reminder_hour": 19,
                "timezone": "Europe/Kyiv",
                "daily_llm_calls": 0,
                "created_at": "2026-11-24T12:00:00Z",
            },
        ),
    )
    answer_route = respx.post(f"{BASE_URL}/api/sessions/42/answer").mock(
        return_value=httpx.Response(500, json={"detail": "Should not be called"}),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=_make_settings())
        key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
        await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
        await dispatcher.storage.set_data(key, {"session_id": 42})

        bot = MagicMock()
        bot.id = BOT_ID
        sent_payloads: list[object] = []

        async def _capture_bot_call(method: object, request_timeout: int | None = None) -> None:
            sent_payloads.append(method)

        bot.side_effect = _capture_bot_call

        start_msg = Message(
            message_id=11,
            date=datetime(2026, 11, 24, 12, 1, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
            text="/start",
        )
        await dispatcher.feed_update(bot, Update(update_id=2, message=start_msg))

    assert profile_route.called
    assert not answer_route.called
    assert len(sent_payloads) == 2
    assert "Вітаю!" in getattr(sent_payloads[0], "text", "")


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503), "недоступний"),
        (APIUnavailableError(TIMEOUT_ERROR_MESSAGE_UA, status_code=None), "довго"),
    ],
)
async def test_answer_handles_rate_limit_and_unavailable_preserving_waiting_answer(
    exc: RateLimitError | APIUnavailableError,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(session_id=42)

    api_client = _make_api_mock()
    api_client.submit_answer.side_effect = exc
    message = _make_message("Ich gehe nach Hause.")

    await handle_exercise_answer(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    message.answer.assert_awaited_once()
    reply = message.answer.await_args.args[0]
    assert expected_fragment in reply.lower()
    if isinstance(exc, APIUnavailableError):
        assert TEMPORARY_RETRY_HINT_UA in reply
    # Critical: keep waiting_answer and session_id intact so the user can retry.
    assert await state.get_state() == TrainingStates.waiting_answer.state
    assert (await state.get_data())["session_id"] == 42


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=502), "недоступний"),
    ],
)
async def test_start_mix_handles_429_and_5xx_gracefully(
    exc: RateLimitError | APIUnavailableError,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    api_client = _make_api_mock()
    api_client.start_session.side_effect = exc
    callback = _make_callback(TRAIN_MODE_MIX_CALLBACK)

    await handle_start_mix_mode(
        callback=callback,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    callback.answer.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    assert expected_fragment in callback.message.answer.await_args.args[0].lower()
    assert await state.get_state() is None


async def test_dispatcher_registers_start_before_training_router() -> None:
    api_client = _make_api_mock()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    router_names = [r.name for r in dispatcher.sub_routers]
    assert "start" in router_names
    assert "training" in router_names
    assert router_names.index("start") < router_names.index("training")
