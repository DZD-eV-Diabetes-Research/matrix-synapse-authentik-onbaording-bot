# Onbot

A bot that keeps a **Matrix (Synapse)** homeserver continuously in sync with an
**[Authentik](https://goauthentik.io/)** identity provider, and onboards every new user into the
right rooms with a friendly welcome. Authentik is the source of truth; Matrix mirrors it
(group → room, group membership → room membership, power levels), and new users get guided in.

See [`GOALS.md`](GOALS.md) for what it aims to do and [`BATTLE_PLAN.md`](BATTLE_PLAN.md) for how
we're building it.

## Status

🚧 **Early — under active rebuild.** This is a clean-slate rewrite of a pre-Matrix-2.0 bot,
targeting modern Matrix (MAS / next-gen auth, authenticated media, sliding sync). The skeleton,
tooling and architecture decisions ([`docs/adr/`](docs/adr/)) are in place; the domain logic is
being ported phase by phase. The CLI commands are stubs for now. The original code is kept in
[`legacy/`](legacy/) as a porting reference only — don't run it.

## Develop

Requires **Python 3.14** and **[PDM](https://pdm-project.org/)**.

```bash
pdm install                 # create the venv and install deps (incl. dev)
pdm run pre-commit install  # enable lint + secret-scan hooks
```

## Run

```bash
pdm run onbot --help        # see available commands
pdm run onbot reconcile-once
```

> Commands currently exit "not implemented yet" — behaviour lands in upcoming phases.

## Test & check

```bash
pdm run pytest -m "not integration"   # fast unit + contract tests
pdm run ruff check .                   # lint
pdm run ruff format --check .          # formatting
pdm run mypy onbot                     # type check
```

## License

MIT — see [`LICENSE`](LICENSE).
