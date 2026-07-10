from agenticiam import cli, gui


def test_gui_main_no_args_launches_browser_gui(monkeypatch):
    called = []
    monkeypatch.setattr(gui, "launch_gui", lambda: called.append(True))
    gui.main(argv=[])
    assert called == [True]


def test_gui_main_with_args_dispatches_to_cli(monkeypatch):
    """This is the fix for a real bug: users who only have the GUI
    executable need `agenticiam-gui mcp` to actually run the MCP stdio
    server rather than silently ignoring the argument and opening a
    browser, which Goose would see as a program that never speaks the
    MCP protocol."""
    calls = []
    monkeypatch.setattr(cli, "main", lambda args=None, **kwargs: calls.append(args))
    gui.main(argv=["mcp"])
    assert calls == [["mcp"]]


def test_gui_main_defaults_to_sys_argv(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agenticiam-gui", "init"])
    calls = []
    monkeypatch.setattr(cli, "main", lambda args=None, **kwargs: calls.append(args))
    gui.main()
    assert calls == [["init"]]
