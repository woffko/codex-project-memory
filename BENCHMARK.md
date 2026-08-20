# Adaptive Recall Benchmark

This report records the deterministic fixture result for the v0.5.0
implementation. It is not a production workload claim.

Command:

```bash
python3 plugins/project-memory/scripts/project_memory_bench.py \
  --queries plugins/project-memory/testdata/recall_cases.json \
  --compare legacy,compact,balanced,auto,deep
```

The fixture contains 15 labeled cases covering exact and vague matches,
Russian and English terminology, constraints, a failure pattern, an
architectural decision, a checkpoint, cold attempt history, incomplete legacy
data, conflicts, supersedence, staleness, a safety-sensitive action, and a
no-result query.

| Mode | Completion | Hit@1 | Hit@3 | Mandatory coverage | Average bytes | Estimated tokens | Tool calls | Unsuccessful |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| legacy | 73.33% | 66.67% | 66.67% | 100% | 660.7 | 220.5 | 1.733 | 4 |
| compact | 100% | 80% | 86.67% | 100% | 1,140.3 | 380.5 | 1.000 | 0 |
| balanced | 100% | 80% | 86.67% | 100% | 1,142.3 | 381.1 | 1.000 | 0 |
| auto | 100% | 80% | 86.67% | 100% | 1,225.2 | 408.7 | 1.000 | 0 |
| deep | 100% | 80% | 86.67% | 100% | 1,428.7 | 476.7 | 1.000 | 0 |

Measured conclusions:

- `auto` preserved the fixture quality floor and used one MCP call per case.
- `auto` used fewer estimated response tokens than always-`deep` retrieval.
- The legacy workflow returned smaller responses on average but failed four
  labeled cases and needed more tool calls.
- This fixture does not establish that v0.5.0 always uses fewer tokens than
  legacy search/get. Real projects should run their own labeled cases.

Estimated tokens use the documented conservative UTF-8 byte conversion. They
are not model billing measurements. Correctness comes only from fixture labels,
not passive runtime metrics.
