from scripts import cli_ui


def test_styled_selector_renders_and_selects_first_option():
    import questionary
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text("\r")
        prompt = questionary.select(
            "Choose", choices=[questionary.Choice("Single", value="single")],
            input=pipe, output=DummyOutput(), **cli_ui._style_kwargs(),
        )
        assert prompt.unsafe_ask() == "single"


def test_text_menu_reprompts_on_invalid_input(monkeypatch, capsys):
    monkeypatch.setattr(cli_ui, "_q", lambda: (_ for _ in ()).throw(ImportError()))
    answers = iter(["", "invalid", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert cli_ui.prompt_main_menu() == "single"
    assert "Please enter a number" in capsys.readouterr().out


def test_text_menu_runs_single_ticker_action(monkeypatch):
    from scripts import run_vyuha_agent

    monkeypatch.setattr(cli_ui, "_q", lambda: (_ for _ in ()).throw(AttributeError()))
    monkeypatch.setattr(cli_ui, "prompt_press_any_key", lambda *args: None)
    answers = iter(["1", "reliance", "", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(run_vyuha_agent, "cmd_single", lambda args: calls.append(args.ticker))
    assert cli_ui.run_main_loop() == 0
    assert calls == ["RELIANCE"]


def test_single_ticker_prompts_fall_back_to_plain_input(monkeypatch):
    monkeypatch.setattr(cli_ui, "_q", lambda: (_ for _ in ()).throw(AttributeError("TTY unavailable")))
    answers = iter(["reliance", "", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    args = cli_ui.prompt_single_args()
    assert args.ticker == "RELIANCE"
    assert args.date == ""
    assert args.full is False


def test_menu_falls_back_to_text_when_selector_returns_none(monkeypatch, capsys):
    class FakeQuestionary:
        @staticmethod
        def select(*args, **kwargs):
            class Prompt:
                @staticmethod
                def ask():
                    return None
            return Prompt()

    monkeypatch.setattr(cli_ui, "_q", lambda: (FakeQuestionary, lambda *a, **kw: None))
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli_ui.prompt_main_menu() == "single"
    assert "using text menu" in capsys.readouterr().out
