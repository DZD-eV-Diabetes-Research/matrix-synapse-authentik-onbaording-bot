# NOTE: placeholder image. BATTLE_PLAN.md Phase 8 replaces this with a multi-stage,
# pinned-digest, non-root build using PDM + a healthcheck. This minimal version just
# keeps the image buildable against the new package layout.
FROM python:3.11

WORKDIR /app

COPY . .

RUN pip install .

ENV ONBOT_CONFIG_FILE_PATH='/config/onbot.yaml'

# Uses the console entry point defined in pyproject.toml ([project.scripts]).
CMD ["onbot", "run"]