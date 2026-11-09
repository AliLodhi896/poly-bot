# Security policy

Polybot places real orders with real money when configured to. Anything that could cost an
operator funds is treated as a security issue, not a bug:

- a way to leak a private key, API credential or control token;
- a way to place an unintended order, or to bypass the paper-by-default gates
  (`POLYBOT_LIVE_ENABLED`, `POLYBOT_REWARD_MM_MICRO_LIVE_ENABLED`, the dry-run check,
  the kill switch, the cross-process live lock);
- a way for someone who can reach the control API to act without the configured
  `POLYBOT_CONTROL_TOKEN`;
- a way to weaken or bypass the geoblock check.

## Reporting

Please report privately via **GitHub → Security → Report a vulnerability** (a private security
advisory) rather than opening a public issue. Include what you found, how to reproduce it, and
what an attacker gains.

You can expect an acknowledgement within a few days. Please allow a fix to land before any
public disclosure.

## Out of scope

- Losses caused by market movement, strategy parameters, or Polymarket itself.
- Vulnerabilities in third-party dependencies without a demonstrated impact on Polybot
  (report those upstream — but a heads-up here is welcome).
- Anything requiring a compromised operator machine.
