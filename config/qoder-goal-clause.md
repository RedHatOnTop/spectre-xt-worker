COMPLETION PROTOCOL (box standard, do not skip): work the goal as one unit, then hand off.
(1) Run the unit's verification command and capture its output.
(2) Commit the work. Never push unless the goal itself says so.
(3) Post a completion report to Slack #lobby under your own identity with: what changed
    (commit hashes), the verification result, and the suggested next goal. Run:
    /usr/local/bin/spectre-slack-notify --agent qoder --channel lobby --text '<report>'
(4) Only AFTER the report is posted, call update_goal(status="complete").
Never mark the goal complete before the report exists. If you are blocked, post the blocker
to Slack and stop without marking complete. Do not wait for a human to resume you: finishing
the unit and reporting IS the handoff.
