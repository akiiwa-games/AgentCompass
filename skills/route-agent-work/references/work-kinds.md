# Choosing work_kind

Classify by the deliverable and where the material is:

- Advice on direction → `direction_reasoning`; a staged build plan → `complex_build_plan`.
- The worker changes code → the implementation routes; finds and fixes a bug or reviews code → `debug_review`.
- The worker returns findings or text, and the material fits the packet → `text_in_text_out`.
- The worker returns findings, and must read this machine's files, repository, data or logs → `exploration_analysis`.
- The worker runs a research or strategy packet on this machine's data (backtests, research scripts, a named variant
  change, a report) → `research_packet`. It is research, not software engineering, even though it writes code.
- The worker reads many files to extract one record per item into a table or inventory → `bounded_bulk_work`.

Judging logic, consistency or correctness is `hard`. Wording, translation, summaries and mechanical changes are `routine`.

| Case | work_kind | difficulty |
|---|---|---|
| Review the logic of a document given in full (contradictions, gaps, unsupported claims) | `text_in_text_out` | `hard` |
| Check a document against the code, configuration or data it describes | `exploration_analysis` | `hard` |
| Find contradictions across a folder of local documents | `exploration_analysis` | `hard` |
| Proofread, translate or summarize a document | `text_in_text_out` | `routine` |
| Decide whether a proposed approach is right | `direction_reasoning` | — |
| Review a diff or pull request | `debug_review` | `hard` |
| Find why a test fails and fix it | `debug_review` | `hard` |
| Explain how a module works or map its call paths | `exploration_analysis` | `hard` |
| Analyze local logs or a local dataset and report | `exploration_analysis` | `hard` |
| Run a research or strategy packet (e.g. a quantitative research packet: backtest, decompose, one named fix, report) | `research_packet` | `hard` |
| Change the project's backtest core or production code that research relies on | `general_implementation` | `hard` |
| Read many local files and list one row per item (inventory, catalogue, CSV of results) | `bounded_bulk_work` | `hard` |
| Implement an agreed feature or refactor | `general_implementation` | `hard` |
| Write a standalone script or module with tests from a spec in the packet | `self_contained_code` | `hard` |
| Apply one small change across many files | `bounded_bulk_work` | `routine` |
| Run an agreed deployment or API sequence | `tool_execution` | `routine` |
| Gather prices, versions or documentation from the web | `web_research` | `routine` |
| Interpret a screenshot or chart | `visual_analysis` | `hard` |

Work that changes kind midway is two tasks: an analysis followed by a fix is `exploration_analysis`, then
`debug_review`.
