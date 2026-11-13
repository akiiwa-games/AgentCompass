# Contributing

Rules, the module ownership map, architectural invariants, regeneration commands and how to run tests are in [`AGENTS.md`](AGENTS.md). Read it before changing code or documents.

- Work on a branch and send a pull request against `main`; new commits only.
- Run only the tests of the feature you touched (`pytest <test files>`); the full suite runs once before a release.
- Shipped reference data and Markdown that documents commands have regeneration and check commands listed in `AGENTS.md`.
- The repository is public: no personal names, paths, machine ids or e-mail addresses in tracked files.
- To report a security problem, follow [`SECURITY.md`](SECURITY.md) instead of opening an issue.
