# folder_actions image. Reuses the FileEngine Python client from the sibling
# python_interface/, so build with the *parent* (monorepo) directory as context:
#   podman build -f folder_actions/Containerfile -t folder-actions ..
#   podman run --rm -p 8099:8099 --env-file folder_actions/.env folder-actions
# A separate command runs the event worker / reconcile sweep off the same image:
#   ... folder-actions-consumer        ... folder-actions-reconcile
FROM python:3.12-slim

WORKDIR /app

# Reused gRPC client FIRST (changes rarely -> better layer caching), then this service.
# The .env (credentials) is never copied.
COPY python_interface/ /app/python_interface/
COPY folder_actions/pyproject.toml folder_actions/README.md /app/folder_actions/
COPY folder_actions/src/ /app/folder_actions/src/
COPY folder_actions/migrations/ /app/folder_actions/migrations/

RUN pip install --no-cache-dir /app/python_interface && \
    pip install --no-cache-dir /app/folder_actions

# Bind all interfaces INSIDE the container (the host still fronts loopback per §9).
ENV FA_HTTP_HOST=0.0.0.0 \
    FA_HTTP_PORT=8099
EXPOSE 8099

CMD ["folder-actions"]
