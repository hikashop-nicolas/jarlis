"""Tests for bootstrap.py: drive with stub backends and synthetic emails."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from jarlis import bootstrap, memory
from jarlis.config import Config, init_paths
from jarlis.models import Email


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.user.languages = ["en"]
    return cfg


def _email(*, sender, subject="Subj", body="Body content here.", to=("you@you.tld",), days_ago=0) -> Email:
    return Email(
        message_id=f"<{abs(hash((sender, subject, body))) % 10**9}@x>",
        sender=sender.lower(),
        sender_name=sender.split("@")[0],
        to=list(to),
        subject=subject,
        body_text=body,
        date=datetime(2026, 5, 25 - days_ago, 10, 0),
    )


class _Stub:
    name = "stub"

    def __init__(self, text: str = "stub markdown content") -> None:
        self.text = text
        self.text_calls: list[str] = []

    def call_text(self, prompt: str) -> str:
        self.text_calls.append(prompt)
        return self.text

    def call_json(self, prompt: str) -> dict:
        return {}


# ---------- subject normalization ----------------------------------------


def test_normalize_subject_root_strips_prefixes() -> None:
    f = bootstrap._normalize_subject_root
    assert f("Re: Hello") == "hello"
    assert f("RE: Re: Hello") == "hello"
    assert f("Fwd: hello") == "hello"
    assert f("[ACME] Re: Grants") == "grants"
    assert f("[Tag] [Other] foo") == "foo"


# ---------- end-to-end bootstrap with stub backend -----------------------


def _make_received(n_per_sender: dict[str, int]) -> list[Email]:
    out: list[Email] = []
    for sender, n in n_per_sender.items():
        for i in range(n):
            out.append(_email(sender=sender, subject=f"Re: Topic {i % 3}", body=f"hello {i}", days_ago=i))
    return out


def _make_sent(n: int) -> list[Email]:
    return [
        _email(
            sender="me@me.tld",
            subject=f"Reply {i}",
            body="Hi friend, here is a reply with reasonable length and useful content. " * 3,
            to=[f"r{i}@x.com"],
            days_ago=i,
        )
        for i in range(n)
    ]


def test_bootstrap_writes_voice_people_topics_and_org() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        received = _make_received({
            "alice@x.com": 5,   # frequent + topic clusters
            "bob@y.com": 4,
            "rare@z.com": 1,    # below threshold; should be skipped
        })
        sent = _make_sent(8)
        backend = _Stub("# stub-generated content\n\nstub body")
        report = bootstrap.bootstrap(
            cfg,
            backend=backend,
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )

        # Voice
        assert report.voice_paths
        assert (cfg.memory_dir / "voice" / "en" / "exemplar_01.md").exists()

        # People: 2 frequent senders meet threshold
        assert len(report.people_paths) == 2
        assert (cfg.memory_dir / "people" / "alice_at_x_com.md").exists()
        assert (cfg.memory_dir / "people" / "bob_at_y_com.md").exists()
        assert not (cfg.memory_dir / "people" / "rare_at_z_com.md").exists()

        # Topics: 3 clusters of 3 ('topic 0/1/2'), each with 3 emails (alice 5 gives 2/2/1, bob 4 gives 2/1/1)
        # Some clusters may not hit threshold; just assert at least one was written.
        assert report.topic_paths

        # Organization + me written
        assert report.organization_path == "memory/00_organization.md"
        assert report.me_path == "memory/me.md"
        assert (cfg.memory_dir / "00_organization.md").exists()
        assert (cfg.memory_dir / "me.md").exists()


def test_bootstrap_no_ai_only_writes_voice_and_ignored_suggestions() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        received = [
            _email(sender="newsletter@bigco.com", subject="Newsletter: May edition", body="x" * 200),
            _email(sender="newsletter@bigco.com", subject="Newsletter: June edition", body="y" * 200, days_ago=1),
            _email(sender="alice@x.com", subject="Hi", body="hello"),
        ]
        sent = _make_sent(3)
        report = bootstrap.bootstrap(
            cfg,
            backend=None,        # no AI
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )
        # Voice + ignored suggestions written; people/topics/me/org skipped
        assert report.voice_paths
        assert report.people_paths == []
        assert report.topic_paths == []
        assert report.organization_path is None
        assert report.me_path is None
        assert report.ignored_topics_path == "memory/ignored_topics.md"
        ignored = memory.load_section(cfg, "ignored_topics")
        assert "newsletter@bigco.com" in ignored


def test_bootstrap_handles_backend_errors_gracefully() -> None:
    class _Failing:
        name = "fail"
        def call_text(self, prompt: str) -> str:
            from jarlis.ai import AIError
            raise AIError("boom")
        def call_json(self, prompt: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        received = _make_received({"alice@x.com": 5})
        sent = _make_sent(5)
        report = bootstrap.bootstrap(
            cfg,
            backend=_Failing(),
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )
        # Voice still works (no AI)
        assert report.voice_paths
        # AI-driven steps logged failures; nothing crashed
        assert report.people_paths == []
        assert report.organization_path is None
        assert any("people" in f or "extraction" in f for f in report.failures)


def test_is_user_voice_via_from_header() -> None:
    e = Email(message_id="<m@x>", sender="me@source.tld", body_text="hi", subject="x")
    assert bootstrap._is_user_voice(e, addresses={"me@source.tld"}, name_markers=["Alice"])


def test_is_user_voice_via_body_head_signature() -> None:
    """Japanese 'タナカより [recipient]さんへ' opening identifies the writer."""
    e = Email(
        message_id="<m@x>",
        sender="team@acme.example",  # shared address
        body_text="タナカより 佐藤さんへ\n\n本日の議事録についてご連絡します。\n\nよろしくお願いします。",
        subject="議事録について",
    )
    assert bootstrap._is_user_voice(
        e,
        addresses={"me@source.tld"},
        name_markers=["タナカ", "Alice"],
    )


def test_is_user_voice_via_body_tail_signature() -> None:
    """A western 'Best, Alice' sign-off in the tail identifies the writer."""
    e = Email(
        message_id="<m@x>",
        sender="team@acme.example",
        body_text=(
            "Hi everyone,\n\nHere is the next meeting agenda. Please review and "
            "share any questions before Friday.\n\nThanks,\nAlice"
        ),
        subject="Next meeting",
    )
    assert bootstrap._is_user_voice(
        e,
        addresses={"me@source.tld"},
        name_markers=["Alice", "Smith"],
    )


def test_is_user_voice_negative_when_other_sender_no_signature() -> None:
    """An email from someone else with no user-name in body should not match."""
    e = Email(
        message_id="<m@x>",
        sender="team@acme.example",
        body_text="佐藤より 皆さんへ\n\n来週の予定をお知らせします。\n\n佐藤",
        subject="来週",
    )
    assert not bootstrap._is_user_voice(
        e,
        addresses={"me@source.tld"},
        name_markers=["Alice", "タナカ"],
    )


def test_is_user_voice_no_markers_falls_back_to_addresses_only() -> None:
    e_from_user = Email(message_id="<m@x>", sender="me@source.tld", body_text="hi", subject="x")
    e_from_other = Email(message_id="<m@x>", sender="team@acme.example", body_text="hi", subject="x")
    assert bootstrap._is_user_voice(e_from_user, addresses={"me@source.tld"}, name_markers=[])
    assert not bootstrap._is_user_voice(e_from_other, addresses={"me@source.tld"}, name_markers=[])


def test_is_user_voice_skips_short_single_letter_markers() -> None:
    """Single-letter names cause too many false positives; skip them at extraction time."""
    cfg = Config()
    cfg.user.firstname = "A"   # too short
    cfg.user.lastname = "Smith"
    markers = bootstrap._user_name_markers(cfg)
    assert "A" not in markers
    assert "Smith" in markers


def test_voice_pool_includes_body_signed_emails_from_shared_address() -> None:
    """End-to-end: a shared-address email signed タナカ counts as user voice."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.user.email = "me@source.tld"
        cfg.user.firstname_alt = "タナカ"
        cfg.user.email_aliases = []

        # Body must clear voice.MIN_BODY_CHARS (80). Pad with a typical
        # second paragraph to make this a realistic exemplar.
        japanese_signed_body = (
            "タナカより 皆さんへ\n\n"
            "今日の会議の資料を共有します。お忙しいところ恐縮ですが、ご確認をお願い致します。\n\n"
            "また、来週の打ち合わせの日程についてもご相談したく存じます。"
            "ご都合の良い時間帯をいくつかお知らせいただけますと幸いです。\n\n"
            "よろしくお願いいたします。\nタナカ"
        )
        received = [
            _email(sender="team@acme.example", body=japanese_signed_body, subject="会議資料"),
            _email(sender="team@acme.example", body="someone-else's body", subject="other"),
        ]
        sent: list = []

        report = bootstrap.bootstrap(
            cfg,
            backend=None,
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )
        # Only the タナカ-signed message counts as voice
        assert report.sent_count == 1
        assert "ja" in report.voice_paths


def test_extract_body_signature_name_basic() -> None:
    body = "タナカより 皆さんへ\n\n本日もよろしくお願いします。\n\nタナカ"
    assert bootstrap.extract_body_signature_name(body) == "タナカ"


def test_extract_body_signature_name_returns_none_when_absent() -> None:
    assert bootstrap.extract_body_signature_name("Hi everyone, no signature here.") is None
    assert bootstrap.extract_body_signature_name("") is None


def test_extract_body_signature_name_requires_both_open_and_close() -> None:
    """``<word>より`` without a matching closing-name signature must NOT match.
    Avoids the 'from <thing>' false positive seen with sentences like
    '先日のメールより…' that aren't signatures."""
    body_open_only = "先日のメールより、新しい情報をお知らせします。詳細は別途共有します。よろしくお願いします。"
    assert bootstrap.extract_body_signature_name(body_open_only) is None


def test_extract_body_signature_name_requires_close_match() -> None:
    """If body opens 'タナカより' but ends with something else, no match."""
    body = "タナカより 皆さんへ\n\n本日もよろしくお願いします。\n\nサトウ"
    # 'タナカ' doesn't appear in the tail → no match
    assert bootstrap.extract_body_signature_name(body) is None


def test_extract_body_signature_handles_trailing_footer() -> None:
    """Many organizations append a long ASCII footer AFTER the signature.
    The signature-line check must look for name-on-own-line, not last-N-chars."""
    body = (
        "タナカより 皆さんへ\n\n"
        "お疲れ様です。会議の件、よろしくお願いします。\n\n"
        "よろしくお願いいたします。\n"
        "タナカ\n"
        "\n"
        "***************************************\n"
        "ASSOCIATION DES PARENTS\n"
        "MAIL: team@acme.example\n"
        "***************************************"
    )
    assert bootstrap.extract_body_signature_name(body) == "タナカ"


def test_extract_western_signature_basic_french() -> None:
    body = (
        "Bonjour Bob,\n\n"
        "Je te confirme que le budget sera revu lundi prochain. "
        "N'hésite pas si tu as des questions.\n\n"
        "Cordialement,\n"
        "Alice Smith"
    )
    assert bootstrap.extract_western_signature_name(body) == "Alice Smith"


def test_extract_western_signature_basic_english() -> None:
    body = "Hi team, here is the update.\n\nBest,\nAlice"
    assert bootstrap.extract_western_signature_name(body) == "Alice"


def test_extract_western_signature_handles_comma_after_closing() -> None:
    body = "Body content here\n\nKind regards,\nAlice Smith"
    assert bootstrap.extract_western_signature_name(body) == "Alice Smith"


def test_extract_western_signature_recognizes_french_compound_closings() -> None:
    """'Bien cordialement,' / 'Très cordialement,' are very common French
    sign-offs and must be recognized."""
    for closing in ("Bien cordialement", "Très cordialement", "Tres cordialement"):
        body = f"Body of email here.\n\n{closing},\nAlice Smith"
        assert bootstrap.extract_western_signature_name(body) == "Alice Smith", closing


def test_extract_western_signature_no_closing_keyword_returns_none() -> None:
    body = "Hi there, just a quick note. Thanks for everything."
    assert bootstrap.extract_western_signature_name(body) is None


def test_extract_western_signature_skips_when_followed_by_non_name() -> None:
    body = "Body.\n\nBest,\nlowercase not a name"
    # First non-empty line after closing isn't capital-cased → not a name
    assert bootstrap.extract_western_signature_name(body) is None


def test_extract_signature_name_tries_both_languages() -> None:
    ja = "タナカより 皆さんへ\n\n本文。\n\nタナカ"
    fr = "Body.\n\nCordialement,\nAlice Smith"
    assert bootstrap.extract_signature_name(ja) == "タナカ"
    assert bootstrap.extract_signature_name(fr) == "Alice Smith"
    assert bootstrap.extract_signature_name("") is None


def test_extract_body_signature_rejects_mid_body_mention() -> None:
    """A name appearing mid-prose (not on its own line) should not count as a sig."""
    body = (
        "タナカより 皆さんへ\n\n"
        "サトウのプロジェクトについてご相談があります。"
        "詳細は別途共有いたします。よろしくお願いします。"
    )
    # 'タナカ' only appears in the opener, never on its own line afterward
    assert bootstrap.extract_body_signature_name(body) is None


def test_extract_body_addressees_basic_bracket_header() -> None:
    body = "【サトウより スズキさん、会計さん、タナカさんへ】\n本文ここから"
    out = bootstrap.extract_body_addressees(body)
    assert out == ["スズキ", "会計", "タナカ"]


def test_extract_body_addressees_strips_various_honorifics() -> None:
    body = "【サトウより スズキさん 田中先生 山田様 佐々木殿へ】\n本文"
    out = bootstrap.extract_body_addressees(body)
    assert "スズキ" in out
    assert "田中" in out
    assert "山田" in out
    assert "佐々木" in out


def test_extract_body_addressees_no_salutation_at_all_returns_empty() -> None:
    """A body with no opener (no bracket, no <name>様, no Dear/Hi/Bonjour) yields []."""
    body = "Just diving straight into the substance.\n\nBody content here."
    assert bootstrap.extract_body_addressees(body) == []


def test_extract_body_addressees_only_top_of_body() -> None:
    """A bracket header buried 10 lines down doesn't count: only the top."""
    body = "Line1\nLine2\nLine3\nLine4\nLine5\nLine6\n【サトウより スズキさんへ】"
    assert bootstrap.extract_body_addressees(body) == []


def test_extract_body_addressees_handles_no_brackets() -> None:
    """Some senders write the convention without 【】 brackets."""
    body = "サトウより スズキさん、タナカさんへ\n本文"
    out = bootstrap.extract_body_addressees(body)
    assert out == ["スズキ", "タナカ"]


def test_user_is_in_addressees_returns_true_for_user_name() -> None:
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname_alt = "タナカ"
    assert bootstrap.user_is_in_addressees(cfg, ["スズキ", "タナカ"]) is True


def test_user_is_in_addressees_returns_false_when_user_absent() -> None:
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname_alt = "タナカ"
    assert bootstrap.user_is_in_addressees(cfg, ["スズキ", "会計", "ナカムラ"]) is False


def test_user_is_in_addressees_treats_minasan_as_everyone() -> None:
    """``皆`` / ``各位`` (post-extraction tokens) means the user IS addressed.

    The extractors strip ``さん`` / ``様`` honorifics, so the in-list tokens
    are bare ``皆`` (from ``皆さん`` / ``皆様``).
    """
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname_alt = "タナカ"
    assert bootstrap.user_is_in_addressees(cfg, ["スズキ", "皆"]) is True
    assert bootstrap.user_is_in_addressees(cfg, ["各位"]) is True


def test_user_is_in_addressees_rejects_specific_group_with_mina() -> None:
    """``イベント係の皆`` ("everyone in the events committee") is a SPECIFIC
    group, not universal everyone. If the user isn't in that committee,
    ``user_is_in_addressees`` must return False — otherwise we keep
    drafting replies for groups the user doesn't belong to.
    """
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname_alt = "タナカ"
    assert bootstrap.user_is_in_addressees(cfg, ["イベント係の皆"]) is False
    assert bootstrap.user_is_in_addressees(cfg, ["関係各位"]) is False


def test_extract_body_addressees_japanese_formal_salutation() -> None:
    """Japanese letter-style opening: ``<role> <name><honorific>`` (no bracket)."""
    body = (
        "運営委員会\n"
        "会計 スズキ様\n"
        "\n"
        "いつもお世話になっております。\n"
        "..."
    )
    out = bootstrap.extract_body_addressees(body)
    assert "スズキ" in out


def test_extract_body_addressees_comma_separated_salutation() -> None:
    body = "タナカ様、サトウ様\n\n本文..."
    out = bootstrap.extract_body_addressees(body)
    assert "タナカ" in out
    assert "サトウ" in out


def test_extract_body_addressees_ignores_otsukaresama_pleasantry() -> None:
    """``お疲れ様です`` is a pleasantry; ``お疲れ`` is NOT an addressee."""
    body = (
        "お疲れ様です。\n"
        "タナカ先生\n"
        "\n"
        "本文..."
    )
    out = bootstrap.extract_body_addressees(body)
    assert "お疲れ" not in out
    assert "タナカ" in out


def test_extract_body_addressees_western_dear_single_name() -> None:
    body = "Dear Alice,\n\nThanks for sending the report."
    assert bootstrap.extract_body_addressees(body) == ["Alice"]


def test_extract_body_addressees_western_hi_with_and() -> None:
    body = "Hi Alice and Bob,\n\nQuick update on the project."
    out = bootstrap.extract_body_addressees(body)
    assert out == ["Alice", "Bob"]


def test_extract_body_addressees_french_bonjour_comma_separated() -> None:
    body = "Bonjour Alice, Bob,\n\nVoici le compte-rendu."
    out = bootstrap.extract_body_addressees(body)
    assert "Alice" in out
    assert "Bob" in out


def test_extract_body_addressees_french_cher_with_et() -> None:
    body = "Cher Alice et Bob,\n\nMerci pour votre message."
    out = bootstrap.extract_body_addressees(body)
    assert "Alice" in out
    assert "Bob" in out


def test_extract_body_addressees_strips_western_titles() -> None:
    body = "Dear Mr. Smith and Mme Dupont,\n\nThank you for…"
    out = bootstrap.extract_body_addressees(body)
    assert "Smith" in out
    assert "Dupont" in out


def test_extract_body_addressees_everyone_salutation_marker() -> None:
    """``Hi everyone,`` / ``Bonjour tous,`` produces the canonical token
    ``"everyone"`` so ``user_is_in_addressees`` returns True (group mail).
    """
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname = "Carol"
    out_en = bootstrap.extract_body_addressees("Hi everyone,\n\nBody")
    out_fr = bootstrap.extract_body_addressees("Bonjour tous,\n\nCorps")
    assert "everyone" in out_en
    assert "everyone" in out_fr
    assert bootstrap.user_is_in_addressees(cfg, out_en) is True
    assert bootstrap.user_is_in_addressees(cfg, out_fr) is True


def test_extract_body_addressees_western_user_not_in_list() -> None:
    """If the user's name isn't among the western addressees, return False."""
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname = "Carol"
    body = "Dear Alice and Bob,\n\nBody"
    out = bootstrap.extract_body_addressees(body)
    assert bootstrap.user_is_in_addressees(cfg, out) is False


def test_extract_body_addressees_skips_mid_body_name_mentions() -> None:
    """Names appearing after line 5 (mid-body prose) shouldn't false-match."""
    body = "Subject opener\n\n\n\n\nタナカさんに連絡しました。"
    out = bootstrap.extract_body_addressees(body)
    assert out == []


def test_user_is_in_addressees_returns_none_for_empty_list() -> None:
    """No bracket header → no signal; classifier shouldn't lean either way."""
    from jarlis.config import Config
    cfg = Config()
    cfg.user.firstname_alt = "タナカ"
    assert bootstrap.user_is_in_addressees(cfg, []) is None


def test_effective_sender_uses_address_for_individual_mailbox() -> None:
    e = Email(message_id="<m@x>", sender="alice@x.com", body_text="hello", subject="x")
    key, kind = bootstrap.effective_sender(e, shared_addresses=set())
    assert key == "alice@x.com"
    assert kind == "address"


def test_effective_sender_extracts_name_from_shared_address() -> None:
    e = Email(
        message_id="<m@x>",
        sender="team@acme.example",
        body_text="タナカより 皆さんへ\n\n本文。\n\nタナカ",
        subject="meeting",
    )
    key, kind = bootstrap.effective_sender(e, shared_addresses={"team@acme.example"})
    assert key == "タナカ"
    assert kind == "name"


def test_effective_sender_falls_back_to_address_when_no_signature() -> None:
    e = Email(
        message_id="<m@x>",
        sender="team@acme.example",
        body_text="Hi all, no signature convention used here.",
        subject="x",
    )
    key, kind = bootstrap.effective_sender(e, shared_addresses={"team@acme.example"})
    assert key == "team@acme.example"
    assert kind == "address"


def test_autodetect_treats_name_order_variants_as_same_person() -> None:
    """An individual signing 'Alice Smith' AND 'Smith Alice' is one person,
    NOT two. The address should NOT be flagged as shared."""
    received = [
        Email(message_id=f"<a{i}>", sender="alice@x", body_text=f"Body.\n\nBest,\nAlice Smith", subject=f"a{i}")
        for i in range(2)
    ] + [
        Email(message_id=f"<b{i}>", sender="alice@x", body_text=f"Body.\n\nBest,\nSmith Alice", subject=f"b{i}")
        for i in range(2)
    ]
    detected = bootstrap.autodetect_shared_addresses(received)
    assert "alice@x" not in detected


def test_autodetect_shared_addresses_finds_multi_signer_addresses() -> None:
    """An address with 2+ distinct in-body signers is auto-detected as shared."""
    received = [
        Email(message_id=f"<a{i}>", sender="team@x", body_text="タナカより 皆さんへ\n\n本文。\n\nタナカ", subject=f"a{i}")
        for i in range(2)
    ] + [
        Email(message_id=f"<b{i}>", sender="team@x", body_text="サトウより 皆さんへ\n\n本文。\n\nサトウ", subject=f"b{i}")
        for i in range(2)
    ] + [
        Email(message_id="<solo>", sender="solo@y", body_text="アリスより 皆さんへ\n\n本文。\n\nアリス", subject="solo")
    ]
    detected = bootstrap.autodetect_shared_addresses(received)
    # team@x has 2 distinct signers -> shared
    assert "team@x" in detected
    # solo@y has only 1 signer -> not shared
    assert "solo@y" not in detected


def test_autodetect_shared_addresses_ignores_unsigned_mail() -> None:
    """Address gets no entries when no body signatures are present."""
    received = [
        Email(message_id=f"<a{i}>", sender="team@x", body_text="No convention here.", subject=f"a{i}")
        for i in range(5)
    ]
    detected = bootstrap.autodetect_shared_addresses(received)
    assert detected == set()


def test_bootstrap_persists_auto_detected_shared_addresses() -> None:
    """Bootstrap writes auto-detected addresses to memory/auto_shared_addresses.txt."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.pipeline.shared_addresses = []  # nothing configured
        # 4 emails: 2 from タナカ, 2 from サトウ, all via team@acme.example
        received = [
            _email(
                sender="team@acme.example",
                subject=f"会議 {i}",
                body=f"タナカより 皆さんへ\n\n本日の議事録についてご連絡します。詳細は別途共有いたします。\n\nタナカ",
                days_ago=i,
            )
            for i in range(2)
        ] + [
            _email(
                sender="team@acme.example",
                subject=f"予定 {i}",
                body=f"サトウより 皆さんへ\n\n来週の予定をお知らせします。ご確認のほどよろしくお願いいたします。\n\nサトウ",
                days_ago=i + 5,
            )
            for i in range(2)
        ]
        backend = _Stub("# Person profile")
        bootstrap.bootstrap(
            cfg,
            backend=backend,
            fetch=False,
            received_emails=received,
            sent_emails=[],
        )
        # Auto-detected file written
        from jarlis import memory as _m
        path = _m.shared_addresses_path(cfg)
        assert path.exists()
        loaded = _m.load_shared_addresses(cfg)
        assert "team@acme.example" in loaded


def test_extract_people_groups_by_effective_sender() -> None:
    """Two distinct senders posting from the same shared address should
    end up in separate memory files, keyed by their in-body name."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        # Leave config empty; rely on auto-detection
        cfg.pipeline.shared_addresses = []
        # 2 messages from "タナカ" via shared
        # 2 messages from "サトウ" via shared
        # Both groups should produce a separate people file
        received = [
            _email(
                sender="team@acme.example",
                subject=f"会議 {i}",
                body=("タナカより 皆さんへ\n\n本日の議事録についてご連絡します。" * 2 + "\n\nタナカ"),
                days_ago=i,
            )
            for i in range(2)
        ] + [
            _email(
                sender="team@acme.example",
                subject=f"予定 {i}",
                body=("サトウより 皆さんへ\n\n来週の予定をお知らせします。" * 2 + "\n\nサトウ"),
                days_ago=i + 5,
            )
            for i in range(2)
        ]
        backend = _Stub("# Person profile\n\nDetails here.")
        report = bootstrap.bootstrap(
            cfg,
            backend=backend,
            fetch=False,
            received_emails=received,
            sent_emails=[],
        )
        assert len(report.people_paths) == 2
        # Both files should be name-keyed (not address-keyed): no "team_at_"
        for p in report.people_paths:
            assert "team_at_acme_example" not in p
            assert "person_" in p  # JP names hash to person_<hash>


def test_voice_pool_includes_self_cc_in_inbox() -> None:
    """Emails the user sent FROM another address but Cc'd to themselves
    show up in the source INBOX, and bootstrap should count them as voice."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.user.email = "me@source.tld"
        cfg.user.email_aliases = ["me@personal.tld"]

        # 5 received emails: 3 from external senders, 2 from one of the user's
        # own addresses (CC-to-self pattern from a different mailbox).
        external_body = "Hi there, here is some external content. " * 3
        own_body_fr = "Bonjour Alice, merci pour ton message. Je te confirme demain. Bien à vous, Alice. " * 2
        received = [
            _email(sender="external@x", body="external 1", subject="X1", days_ago=0),
            _email(sender="external@x", body="external 2", subject="X2", days_ago=1),
            _email(sender="external@x", body="external 3", subject="X3", days_ago=2),
            # From one of the user's OWN addresses → counts as voice
            _email(
                sender="me@personal.tld",
                body=own_body_fr,
                subject="Re: project",
                to=["partner@y.tld"],
                days_ago=3,
            ),
            _email(
                sender="me@source.tld",  # Sometimes the source itself is the From
                body=own_body_fr + " variation",
                subject="Re: budget",
                to=["finance@y.tld"],
                days_ago=4,
            ),
        ]
        sent = _make_sent(2)  # 2 emails from the Sent folder

        report = bootstrap.bootstrap(
            cfg,
            backend=None,
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )
        # sent_count should reflect both the Sent folder AND the self-Cc'd ones
        assert report.sent_count == 2 + 2
        # Voice exemplar should have been written for at least 'fr' (from own_body_fr)
        # or some language: confirm voice/ has files
        assert any(report.voice_paths.values())


def test_voice_pool_ignores_others_in_inbox() -> None:
    """Emails from third parties stay out of the voice pool."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.user.email = "me@source.tld"
        cfg.user.email_aliases = []

        received = [
            _email(sender="someone-else@x", body="x" * 200, subject=f"Subj {i}", days_ago=i)
            for i in range(3)
        ]
        sent: list = []  # No sent folder

        report = bootstrap.bootstrap(
            cfg,
            backend=None,
            fetch=False,
            received_emails=received,
            sent_emails=sent,
        )
        assert report.sent_count == 0  # nobody else's voice counts as ours
        assert report.voice_paths == {}


def test_suggest_ignored_finds_automated_senders() -> None:
    received = [
        _email(sender="newsletter@x.com", subject="May newsletter"),
        _email(sender="newsletter@x.com", subject="June newsletter"),
        _email(sender="real-person@y.com", subject="Hi"),
    ]
    out = bootstrap._suggest_ignored(received)
    joined = "\n".join(out)
    assert "newsletter@x.com" in joined
