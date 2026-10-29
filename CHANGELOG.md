# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Space AcoustID request starts to respect the public service's documented request rate when the scanner runs concurrent workers.
- Keep Whisper transcription dependencies optional instead of installing them in every base deployment.

### Fixed

- Reduce the risk of AcoustID throttling during concurrent scans.
- Avoid installing the heavyweight `faster-whisper` dependency when the lyrics fallback is not needed.
