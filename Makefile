.PHONY: db test smoke pilot main lint clean
db:     ; docker compose up -d && until docker compose exec -T db psql -U postgres -d retail -tAc "SELECT 1 FROM edge.orders LIMIT 1" >/dev/null 2>&1; do sleep 2; done
test:   ; python -m pytest -q
smoke:  ; python -m sql_agent run configs/smoke.yaml
pilot:  ; python -m sql_agent run configs/pilot.yaml
main:   ; python -m sql_agent run configs/main.yaml
lint:   ; ruff check .
clean:  ; rm -rf results/smoke .pytest_cache
