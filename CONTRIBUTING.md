# Contributing

- Tests first. Write the test, see it fail, then make it pass. A change that
  adds behaviour without a test will be asked for one.
- One behaviour per pull request, so each can be reviewed and reverted alone.
- Run the tests and the scan before you push:

  ```
  python3 -m unittest discover -s tests
  python3 scripts/scan.py --tree .
  python3 scripts/scan.py --history
  ```

  The scan fails on tokens, absolute home paths, personal email addresses and
  references to issues in other repositories. Nothing committed here can be
  taken back, so check the history as well as the tree.
- A deny file of your own is optional. If you keep a list of private names you
  never want published, put one term per line in a file outside the checkout
  and pass it with `--deny-file`, or set `SCAN_DENY_FILE`. Matches are
  case-insensitive and are reported by line number, never by value.
- Use a no-reply address for commits.
