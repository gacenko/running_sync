# AI Assistance

This project was built with [Claude](https://claude.ai) (Anthropic).

## What AI helped with

- Boilerplate and scaffolding (GitHub Actions workflow, Cloudflare Worker setup)
- Debugging Garmin API quirks (undocumented FIT fields, sleep date logic)
- Refactoring code structure
- Writing this README

## What I designed and decided

- Overall architecture: Strava → Cloudflare Worker → GitHub Actions → Garmin → Google Drive
- Output JSON schema and what data is worth capturing
- Decision to include per-interval splits, sleep context, and subjective feel per run
- Decision to downsample time series to 10s intervals without per-sample timestamps
- All domain logic around running metrics (what pace.zone targets mean, how Garmin structures workouts)

## Notes

AI was used as a coding assistant throughout — writing and refactoring code based on my direction. All architectural decisions, data modeling, and domain knowledge are my own.
