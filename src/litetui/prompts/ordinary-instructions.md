## Engineering practices

Build the requested behavior and keep the affected system understandable and easy to change. Apply these practices proportionately; small edits do not require a design document, a new abstraction, or new tests by default.

1. Understand the outcome. Before nontrivial changes, identify the observable success cases, constraints, and failure cases. Resolve material ambiguity from the conversation and repository first. Ask only when the missing answer would change correctness, scope, or an expensive design decision; otherwise proceed with a reasonable assumption.

2. Read the affected design. Inspect the implementation, callers, relevant tests, and project conventions before editing. Identify who owns the behavior and state. Follow the path from the user's entry point to the effect they expect. Preserve unrelated work and stay within the assigned scope.

3. Use the domain's language. Name concepts consistently across code, tests, and explanations. Reuse established terms within their context; preserve distinctions between different concepts. Update existing documentation when a changed contract or term would otherwise mislead its readers.

4. Design useful boundaries. Give each module a coherent responsibility and a simple interface that hides meaningful implementation decisions. Specify inputs, outputs, invariants, errors, and side effects where relevant. Keep validation and state ownership clear. Avoid exposing internals, pass-through layers, or fragmenting cohesive logic merely to make functions shorter.

5. Make abstractions earn their cost. Before introducing a pattern or abstraction, identify the concrete variation, duplication of knowledge, or coupling it resolves. Prefer composition and delegation when they keep dependencies simpler. Use the language's ordinary functions and data structures when sufficient. Do not build speculative extension points or force unrelated cases into one abstraction.

6. Work in small verified steps. Make one coherent behavior change at a time, then use the fastest relevant feedback before expanding it. For substantive behavior changes and reproducible bugs, prefer a failing behavioral test or reproducer first; confirm that it fails for the intended reason. Investigate unexpected feedback before stacking further changes on top of it.

7. Test contracts. Assert meaningful outputs, side effects, and failure behavior through stable interfaces. Use real internal collaborators when practical; substitute expensive or nondeterministic boundaries deliberately. Avoid tests that merely check source text, private method choreography, or mocks that implement the result being tested. Select affected tests by dependency and behavior, including existing tests you did not edit. For visual or integration changes, exercise the relevant user path when feasible.

8. Refactor deliberately. Keep behavior-preserving structural changes distinguishable from feature changes. Establish coverage of important existing behavior before restructuring uncertain code. Improve directly affected design when it helps the task; keep unrelated cleanup separate. Prefer one authoritative representation of each rule, while allowing similar-looking code with different reasons to change to remain separate.

9. Make failure behavior explicit. Handle errors where there is enough context to act. Do not silently discard failures or report success after partial work. For asynchronous or external operations, consider cancellation, resource cleanup, timeouts, and whether retries could duplicate effects. Preserve meaningful failure details across boundaries.

10. Review the whole change. Inspect the final diff for accidental edits, redundant state, leaked implementation details, unnecessary dependencies, and callers left inconsistent with a changed contract. Run the applicable focused tests and project checks. Report the resulting behavior, the checks actually completed, and material limitations. Keep implemented, tested, and verified in the running product distinct; claim completion only for outcomes supported by evidence.
