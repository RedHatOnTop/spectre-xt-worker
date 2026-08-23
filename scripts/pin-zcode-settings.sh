#!/bin/bash
# Pin ZCode worker settings on the Spectre. Does not touch credentials or models.
set -euo pipefail

SETTING="${HOME}/.zcode/v2/setting.json"
if [[ ! -f "${SETTING}" ]]; then
  echo "missing ${SETTING} — start ZCode once, then re-run" >&2
  exit 1
fi

tmp="$(mktemp)"
jq '
  .keepAwakeWhileRunning = true
  | .desktopChromiumHardwareAccelerationEnabled = false
  | .autoDownloadAndInstallUpdates = false
  | .receivePreviewUpdates = false
  | .zcodeInteractionBehavior = "queue"
  | .askUserQuestionAutoResolutionEnabled = true
' "${SETTING}" >"${tmp}"

cp -a "${SETTING}" "${SETTING}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
mv "${tmp}" "${SETTING}"

echo "pinned:"
jq '{
  keepAwakeWhileRunning,
  desktopChromiumHardwareAccelerationEnabled,
  autoDownloadAndInstallUpdates,
  zcodeInteractionBehavior
}' "${SETTING}"
