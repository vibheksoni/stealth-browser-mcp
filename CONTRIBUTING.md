## Contributing

Thanks for your interest in improving Stealth Browser MCP. We welcome issues and PRs from the community.

### Ways to contribute

- Report bugs with clear repro steps
- Propose features with concrete user stories
- Improve docs and examples
- Optimize performance or reliability

### Development setup

1. Clone and create a virtualenv
2. Activate the virtualenv and install deps

Windows
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Mac/Linux
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Run the server: `python src/server.py`
4. Run the tests: `python -m unittest discover -s tests -v`. Set `STEALTH_BROWSER_TESTS=1` to include tests that launch a real browser.
5. If your change can affect detection, run the stealth benchmark before and after: `python -m benchmarks.stealth run --mode headed`. See [STEALTH_TESTS.md](STEALTH_TESTS.md) for targets and statuses.

### Pull request guidelines

- Keep PRs focused and under 300 lines of diff when possible
- Add or update docs when behavior changes
- Use clear, descriptive titles following Conventional Commits when feasible (e.g., `feat: add tab suspend/resume API`)
- Link related issues in the PR description

### Issue guidelines

- For bugs, include expected vs actual behavior, steps to reproduce, logs, and environment details
- For features, include the problem, target users, and acceptance criteria

### Code style

- Python 3.10+
- Prefer readable, explicit code and small functions
- Add minimal docstrings for public functions

### Security

If you find a security issue, please follow the process in `SECURITY.md`.


