.PHONY: test preflight status dash dash-demo install-schedule unload-schedule
test:            ; python3 -m pytest qqq/tests/ -q
preflight:       ; python3 -m qqq.orchestrator --mode preflight
status:          ; python3 scripts/status.py
dash:            ; python3 -m dashboard.server
dash-demo:       ; python3 -m dashboard.server --demo
install-schedule:; ./scripts/install_launchd.sh
unload-schedule: ; ./scripts/install_launchd.sh unload
