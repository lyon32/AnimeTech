"""Two bots: the publishing bot only publishes to the channel; the admin bot only talks to administrators."""
import logging

import pytest

from v2_automation import admin_telegram, alerts, app_config, downloader, logsetup, publisher
from v2_automation.app_config import AppConfig, BotCapacity

PUB = "111111:PUBLISHING_BOT_TOKEN_AAAA"
ADM = "222222:ADMIN_BOT_TOKEN_BBBB_ZZZ"


def _cfg(admin_token="", admins=(42,)) -> AppConfig:
    return AppConfig(source={}, queues={}, downloads={}, telegram={"api_base_url": "http://127.0.0.1:8081"},
                     publication={}, limits={}, monitoring={}, logging={}, bot_token=PUB, channel_id="-100CHANNEL",
                     admin_telegram_ids=list(admins), bot_capacity=BotCapacity(True, True, "t", 1, None, None, 200, None),
                     admin_bot_token=admin_token)


class Recorder:
    """Stand-in for V2TelegramClient recording (token, chat) and what it was asked to send."""
    created, sent = [], []

    def __init__(self, token, chat_id, base_url=None, **kw):
        self.token, self.chat_id, self.base_url = token, chat_id, base_url
        Recorder.created.append((token, str(chat_id)))

    def send_message(self, text):
        Recorder.sent.append((self.token, self.chat_id, text))

    def get_me(self):
        return {}


@pytest.fixture(autouse=True)
def _reset():
    Recorder.created, Recorder.sent = [], []


def test_notify_token_falls_back_to_the_single_bot_when_no_admin_bot():
    assert _cfg("").notify_token() == PUB and _cfg(ADM).notify_token() == ADM


def test_alerts_and_daily_summary_use_only_the_admin_bot(monkeypatch):
    monkeypatch.setattr(publisher, "V2TelegramClient", Recorder)
    cfg = _cfg(ADM, admins=(42,))
    alerts.default_dispatcher(cfg)("definitive_failure", "Échec", "corps")
    alerts.default_sender(cfg)("Résumé")
    assert Recorder.sent and all(tok == ADM and chat == "42" for tok, chat, _ in Recorder.sent)
    assert all(tok != PUB for tok, _ in Recorder.created)                # the publishing bot is never used
    assert all(chat != "-100CHANNEL" for _, chat in Recorder.created)     # nor the public channel


def test_single_bot_mode_still_works_without_admin_token(monkeypatch):
    monkeypatch.setattr(publisher, "V2TelegramClient", Recorder)
    alerts.default_dispatcher(_cfg("", admins=(42,)))("retry", "t", "b")
    assert Recorder.sent[0][0] == PUB and Recorder.sent[0][1] == "42"


def test_no_admin_id_means_nothing_is_sent_even_with_two_bots(monkeypatch):
    monkeypatch.setattr(publisher, "V2TelegramClient", Recorder)
    assert alerts.default_dispatcher(_cfg(ADM, admins=())) is None and alerts.default_sender(_cfg(ADM, admins=())) is None
    assert Recorder.created == []


def test_admin_panel_bot_uses_the_admin_token_and_never_the_channel(monkeypatch):
    monkeypatch.setattr(admin_telegram, "V2TelegramClient", Recorder)
    bot = admin_telegram.AdminBot(_cfg(ADM))
    assert Recorder.created == [(ADM, "0")]                              # admin token, no channel id
    assert bot.allowed(42) and not bot.allowed(1)


def test_publication_uses_only_the_publishing_bot_and_the_channel(monkeypatch):
    monkeypatch.setattr(downloader, "V2TelegramClient", Recorder)
    client = downloader.make_telegram_client(_cfg(ADM))
    assert (client.token, str(client.chat_id)) == (PUB, "-100CHANNEL")
    assert all(tok != ADM for tok, _ in Recorder.created)


def test_admin_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_BOT_TOKEN", f"  {ADM}  ")
    monkeypatch.setattr(app_config, "_probe_local_bot_api", lambda base: {"enabled": False})
    cfg = app_config.load_config()
    assert cfg.admin_bot_token == ADM                                   # trimmed, straight from the environment


def test_both_tokens_are_masked_in_logs():
    filters = logsetup.token_filters(_cfg(ADM))
    assert len(filters) == 2
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, f"boom {PUB} and {ADM}", None, None)
    for f in filters:
        f.filter(rec)
    assert PUB not in rec.msg and ADM not in rec.msg
    assert len(logsetup.token_filters(_cfg(""))) == 1                    # single-bot mode: one token


def test_token_filter_survives_a_lone_dict_argument():
    import logging
    from v2_automation.logsetup import _TokenFilter
    f = _TokenFilter("123456:SECRET_SECRET")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "cycle %s", ({"a": 1, "t": "123456:SECRET_SECRET"},), None)
    assert rec.args == {"a": 1, "t": "123456:SECRET_SECRET"}                  # logging unwraps a lone dict
    assert f.filter(rec) and "SECRET_SECRET" not in rec.getMessage() and "***TOKEN***" in rec.getMessage()
