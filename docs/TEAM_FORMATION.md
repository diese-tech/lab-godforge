# Team Formation

GodForge forms two five-player SMITE teams without relying on ForgeLens,
official ranks, or another service. The ready lobby's first and second role
choices are always available. Organizers may additionally supply a skill band,
self-declared experience, and a bounded recent game-night adjustment through
the domain API.

The lobby card offers three accessible, explicit choices:

- **Role Fit Teams** maximizes first choices, then second choices, and reports
  assignments outside either preference as unavoidable fills.
- **Balanced Teams** minimizes the absolute difference between the teams'
  visible strength totals, then uses role satisfaction as a tie-breaker.
- **Captain Teams** selects two captain volunteers deterministically and uses a
  snake pick order that favors uncovered roles before strength and stable ID
  tie-breakers.

All modes assign exactly one Solo, Jungle, Mid, Support, and ADC to each team.
The selected mode, role assignments, preference satisfaction, strength
difference, and captain pick order are retained in the party draft snapshot.
Given the same inputs, results are identical regardless of input ordering.

Strength is intentionally simple and explainable:

```text
organizer skill-band base + min(experience, 100) + recent adjustment
```

Missing skill bands use the intermediate base. This is a recreational
organizer input, not an inferred official rank or a global reputation score.
