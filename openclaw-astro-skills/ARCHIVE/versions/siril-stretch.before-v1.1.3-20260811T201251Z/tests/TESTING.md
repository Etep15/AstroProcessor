# siril-stretch testing checklist

## 1) Verify installation

```bash
ls -R /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-stretch
bash -n /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-stretch/bin/stretch
bash -n /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-stretch/scripts/ghs-pass.sh
bash -n /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-stretch/scripts/black-point-pass.sh
```

## 2) Manual helper smoke tests

```bash
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-stretch/bin/stretch
```

## 3) Skill behavior expectations

The skill should:

1. accept a reviewed starless image from `siril-sho-channel-balance`
2. run at least 2 GHS→BP rounds
3. stop no later than 5 rounds
4. preserve round winners
5. create a final 3-candidate review
6. choose the best balance of brightness, contrast, and color richness
7. hand off to `siril-green-reduction`

## 4) Production test prompt

```text
Process M16 July 2026 with stretch
```

## 5) What to inspect in the result

- nebula stronger than the pre-stretch image
- color richness retained or improved
- background allowed to be somewhat lifted if needed
- no obvious clipping of shadows or highlights
- histogram wider than the input image
- if a later round looks worse, an earlier round should be preserved and allowed to win
