FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv venv /opt/pocket-tts \
    && uv pip install --python /opt/pocket-tts/bin/python \
        torch --index https://download.pytorch.org/whl/cpu \
    && uv pip install --python /opt/pocket-tts/bin/python "pocket-tts==2.1.0"

RUN groupadd --gid 3000 pocket \
    && useradd --uid 3000 --gid 3000 --create-home --shell /usr/sbin/nologin pocket

ENV HOME=/home/pocket \
    PATH=/opt/pocket-tts/bin:$PATH
USER pocket

ENTRYPOINT ["pocket-tts"]
CMD ["serve", "--host", "127.0.0.1", "--port", "8000"]
