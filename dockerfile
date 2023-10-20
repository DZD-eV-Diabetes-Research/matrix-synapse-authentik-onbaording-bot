# specify start image
FROM python:3.11

RUN apt-get update && apt-get install -y libolm-dev

WORKDIR /app

COPY . .

RUN pip install .
# set a default environment variable for the name of your bot
ENV ONBOT_CONFIG_FILE_PATH='/config/onbot.yaml'

# set the start command
CMD [ "python3", "onbot/main.py"]