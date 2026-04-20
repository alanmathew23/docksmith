#!/bin/sh
# app.sh — entry point for the sampleapp container
# APP_NAME is set via ENV in the Docksmithfile (overridable with -e at run-time)
echo "Hello from ${APP_NAME:-docksmith-sample}!"
echo "Running in offline-only container — no network required."
