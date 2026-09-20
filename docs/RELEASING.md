# Releasing Hermes MCP Control Plane

This document defines the public release process.

## Release identity

- Package: `hermes-control-mcp`
- Primary CLI: `hermes-control-mcp`
- Python package: `hermes_control_mcp`
- Current beta line: `0.2.0b3`
- Tag format: `v<version>` (for example `v0.2.0b3`)

## One-time PyPI setup

Use PyPI Trusted Publishing. Do not add a long-lived `PYPI_TOKEN` secret.

1. In GitHub, create an environment named `pypi`.
2. Add the repository owner as a required reviewer for that environment.
3. In PyPI account Publishing settings, create a pending GitHub Actions publisher with:
   - PyPI project: `hermes-control-mcp`
   - Owner: `upmeister`
   - Repository: `hermes-control-mcp`
   - Workflow: `release.yml`
   - Environment: `pypi`
4. Publish promptly after creating the pending publisher: a pending publisher does not reserve the project name until first use.

The release workflow grants `id-token: write` only to the PyPI publish job.

## Prepare a release

**Important:** creating a Git tag does not bump the package version. The version
must already be committed on `main` before the tag is created.

1. Confirm `main` is the exact release candidate and CI is green.
2. Confirm production/deployment smoke is complete when the change affects runtime behavior.
3. Prepare and merge a release-version commit/PR **before tagging**:
   - set the target version in `pyproject.toml`;
   - set the same version in `src/hermes_control_mcp/__init__.py`;
   - move the relevant notes from `[Unreleased]` into a matching
     `## [<version>] - YYYY-MM-DD` section in `CHANGELOG.md`;
   - leave a fresh empty `[Unreleased]` section above it.
4. Re-read those three files from `main` and confirm they all name the exact
   version you are about to tag.
5. Verify no private hostnames, credentials, local vault paths, or deployment secrets are tracked.
6. Verify:

~~~bash
./scripts/test.sh
python -m unittest discover -s tests -v
python -m compileall -q src
python -m py_compile src/hermes_control_mcp/*.py
git diff --check
~~~

## Trigger the release

Create and push the exact version tag from `main`:

~~~bash
git switch main
git pull --ff-only
git tag -a v0.2.0b3 -m "Hermes MCP Control Plane 0.2.0b2"
git push origin v0.2.0b3
~~~

`release.yml` then:

1. validates tag == package version;
2. builds wheel + sdist once;
3. runs package metadata checks;
4. installs the wheel into a fresh virtual environment;
5. runs the installed doctor + MCP stdio smoke;
6. stores the exact distributions as a workflow artifact;
7. waits for approval on the `pypi` environment;
8. publishes those distributions to PyPI with Trusted Publishing;
9. creates a GitHub Release from the same distributions and changelog section.

## Post-release verification

After the workflow succeeds:

~~~bash
python -m venv /tmp/hermes-control-release-check
/tmp/hermes-control-release-check/bin/python -m pip install --upgrade pip
/tmp/hermes-control-release-check/bin/pip install hermes-control-mcp==0.2.0b3
/tmp/hermes-control-release-check/bin/hermes-control-mcp --help
/tmp/hermes-control-release-check/bin/hermes-control-mcp doctor
~~~

Also verify:

- the PyPI project page shows the expected metadata and files;
- the GitHub Release is marked prerelease for beta versions;
- wheel and sdist filenames/version match;
- README install instructions point at the released package;
- no credential values appear in release logs.

## Recover a GitHub Release after PyPI already succeeded

If PyPI publication succeeds but the final GitHub Release job fails, do **not**
rerun the PyPI publish step and do not rebuild the package.

Use the manual `release` workflow recovery mode:

1. open **Actions → release → Run workflow** on `main`;
2. set `release_tag` to the already-published tag, for example `v0.2.0b3`;
3. set `source_run_id` to the original release workflow run that produced the
   verified `release-dist` artifact;
4. run the workflow.

Recovery downloads the original verified wheel/sdist/release notes from that run,
verifies the tag exists and no GitHub Release already exists, then creates only
the GitHub Release.

## If publishing fails

Do not reuse a version with different contents after any distribution file has reached PyPI.

- If validation/build/smoke fails before publish: fix the branch, merge, delete the failed unpublished tag if necessary, and create the tag again only after the release candidate is correct.
- If PyPI rejects Trusted Publishing: correct the PyPI/GitHub publisher configuration; do not add an emergency long-lived token unless the release process is deliberately redesigned.
- If a file for the version was already accepted by PyPI and code must change, bump to a new version (for example `0.2.0b4`).
- If GitHub Release creation fails after PyPI succeeds, recreate the GitHub Release from the exact workflow distributions; do not rebuild the package.

## Release security

The release workflow is intentionally separate from ordinary CI.

- Pull requests never receive PyPI OIDC publishing permission.
- Only the publish job gets `id-token: write`.
- The `pypi` environment supplies the manual approval boundary.
- The build job produces the immutable distributions consumed by both PyPI and GitHub Release jobs.
