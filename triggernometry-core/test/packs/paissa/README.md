# Triggernometry replay fixtures

These folders come from [Paissa Heavy Industries Triggernometry Triggers](https://github.com/paissaheavyindustries/Triggernometry-Triggers/tree/32a9f4e7d4cc6b0d5fe0854e941783bd2752962e/Repositories).
The upstream license is in LICENSE.

- `top-headmarkers.xml` is PlayStation TOP from `ffxiv_ultimate_top.xml`.
- `zelenia-bloom.xml` is D. Bloom 2 from `ffxiv_sharingchannel.xml`.
- `top-party-synergy.xml` is Party Synergy from `ffxiv_ultimate_top.xml`.
- `top-pantokrator.xml` contains the Guided Missile Kyrios and Condensed Wave Cannon Kyrios triggers from that TOP pack.

Only the export wrapper and XML whitespace were changed. Conditions, IDs,
actions, environment variables and delays are preserved. Keeping these small
folders lets the tests replay callouts without loading unrelated HTTP requests
or automarkers from the full packs. Telesto requests from the Party Synergy and
Pantokrator fixtures go to a local test server.
