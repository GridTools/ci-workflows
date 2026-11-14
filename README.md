# GridTools CI workflows

Reusable GitHub Actions workflows shared by GridTools projects and their
partners (for example GT4Py and icon4py).

**Status: pre-release.** Nothing here is released yet. Both workflows ship
together in the first release tag (`v0.1.0`); do not call them with a real
key before it exists. See the [agent review's](docs/agent-review.md#release-checklist)
and [PR Agent's](docs/pr-agent.md#release-checklist) release checklists.

## Workflows

| Workflow | Purpose | Docs |
| --- | --- | --- |
| [`agent-review.yml`](.github/workflows/agent-review.yml) | Advisory pull request review by a coding agent running in a network-restricted sandbox (AWF) | [docs/agent-review.md](docs/agent-review.md) |
| [`pr-agent.yml`](.github/workflows/pr-agent.yml) | Advisory pull request review and comment commands by [PR Agent](https://github.com/The-PR-Agent/pr-agent), from the diff | [docs/pr-agent.md](docs/pr-agent.md) |

The agent review was designed and security-reviewed in GT4Py first; see
[GridTools/gt4py#2985](https://github.com/GridTools/gt4py/pull/2985) for the
design history. The PR Agent workflow is a port of icon4py's
([C2SM/icon4py#1547](https://github.com/C2SM/icon4py/pull/1547)).

## Using a workflow

Call a workflow from your repository pinned by the **full commit SHA** of a
release tag on `main`, with the tag as a comment, and let Dependabot propose
updates:

```yaml
jobs:
  example:
    uses: GridTools/ci-workflows/.github/workflows/<workflow>.yml@<40-hex sha> # vX.Y.Z
```

Never call a branch (`@main`) or a moving tag: a change here would reach your
repository unreviewed. Check every new pin, including Dependabot's, before
merging it: a commit that exists only in a fork resolves through this
repository's name too.

```bash
gh api repos/GridTools/ci-workflows/compare/main...<sha> --jq .status  # 'identical' or 'behind'
```

zizmor's `impostor-commit` audit does the same check.

### Adopter checklist

For the agent review: see
[Required settings in the calling repository](docs/agent-review.md#required-settings-in-the-calling-repository).
For PR Agent: see
[Required settings in the calling repository](docs/pr-agent.md#required-settings-in-the-calling-repository).

## Contributing

Changes go through pull requests against `main`, reviewed by the code owners
(see `.github/CODEOWNERS`). Every action used here is pinned by full commit
SHA.

## License

BSD 3-Clause, see [LICENSE](LICENSE).
