# Agent stress test (tests/agent_eval). Needs the stack running: docker compose up -d
.PHONY: agent-eval agent-eval-dry agent-eval-check agent-eval-report

AGENT_EVAL = uv run --project backend python tests/agent_eval/run.py

agent-eval:          ## every case, real models, judge; ARGS="--category booking --case oos-wifi"
	$(AGENT_EVAL) $(ARGS)

agent-eval-dry:      ## plan and cost estimate only, no requests
	$(AGENT_EVAL) --dry-run $(ARGS)

agent-eval-check:    ## offline: validate cases.yaml and the grader (what CI runs)
	cd backend && uv run pytest tests/test_agent_eval.py -q

agent-eval-report:   ## re-render report.html: RESULTS=... [BEFORE=...]
	uv run --project backend python tests/agent_eval/report.py $(RESULTS) $(if $(BEFORE),--before $(BEFORE))
