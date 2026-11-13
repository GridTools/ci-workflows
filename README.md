# GridTools CI workflows

Reusable GitHub Actions workflows shared by GridTools projects and their
partners (for example GT4Py and icon4py).

**Status: pre-release.** Nothing here is released yet. Do not call these
workflows from production repositories until a `v1.0.0` tag exists.

## Workflows

| Workflow | Purpose | Docs |
| --- | --- | --- |
| [`agent-review.yml`](.github/workflows/agent-review.yml) | Advisory pull request review by a coding agent running in a network-restricted sandbox (AWF) | [docs/agent-review.md](docs/agent-review.md) |

The agent review was designed and security-reviewed in GT4Py first; see
[GridTools/gt4py#2985](https://github.com/GridTools/gt4py/pull/2985) for the
design history.

## Using a workflow

Call a workflow from your repository pinned by **full commit SHA**, with the
release tag as a comment, and let Dependabot propose updates:

```yaml
jobs:
  example:
    uses: GridTools/ci-workflows/.github/workflows/<workflow>.yml@<40-hex sha> # vX.Y.Z
```

Never call a branch (`@main`) or a moving tag: a change here would reach your
repository unreviewed.

### Adopter checklist

For the agent review: see
[Required settings in the calling repository](docs/agent-review.md#required-settings-in-the-calling-repository).

## Contributing

Changes go through pull requests against `main`, reviewed by the code owners
(see `.github/CODEOWNERS`). Every action used here is pinned by full commit
SHA.

## License

BSD 3-Clause, see [LICENSE](LICENSE).
