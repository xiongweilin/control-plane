# Epistemic repair loop

The personal incident-repair profile projects current repair episodes into
`meta-controller` epistemic policy state while preserving Agent Kernel as the
sole owner of durable controller, Work, authorization, effect, verification,
and responsibility semantics.

## Runtime loop

```text
alert / responsibility assessment
    -> ControllerState
    -> RepairEpistemicProfile
       - EpistemicIssue
       - StructuralTension
       - Candidate frontier
       - WorkingSelfModel
       - SearchBudget
    -> MetaControlFrame
    -> advisory meta intent
    -> bounded diagnosis (`reason.generate`)
    -> CognitiveClosure
    -> WorkProposal / admission
    -> Agent Kernel effect boundary
    -> verification
    -> RevisionAssessment
    -> close / wait / explicit reopen
```

A first diagnosis is represented as an evidence-acquisition problem. When
reality contradicts a repair closure and cognition is explicitly reopened, the
profile records candidate-space incompleteness suspicion and repeated-reopen
structural tension. The next diagnosis is therefore instructed to revise the
working distinctions or equivalence relation rather than repeat the previous
root-cause partition unchanged.

The line-ending cleanup case is a concrete representation-revision example:
repository `dirty` state is not treated as a sufficient semantic distinction.
The profile first applies the bounded normalization-equivalence distinction
between semantic content change and representational line-ending noise.

## Authority ceiling

The epistemic profile is non-authority-bearing.

```text
EpistemicIssue       -/-> truth
Candidate            -/-> qualification
MetaControlIntent    -/-> ControllerDecision
MetaControlIntent    -/-> Work
MetaControlIntent    -/-> effect authorization
CapabilityBelief     -/-> capability authority
ProviderSuccess      -/-> target recovery
RepresentationChange -/-> effect execution
```

Effectful epistemic experiments cannot execute during the diagnosis pass. They
must be described as candidates and re-enter the normal path through
`CognitiveClosure -> WorkProposal -> Agent Kernel`.

## Meta-control hard gates

The pinned meta-controller fails closed across the policy/runtime seam:

- candidates and epistemic issues cannot cross controller scope;
- a candidate cannot downgrade a capability's known effect class;
- effectful experiments cannot compile into direct read-class capability calls;
- compiler hooks must return decisions bound to the current controller/version;
- selected intents are kept compiler-reachable rather than emitting incomplete
  read intents with no concrete capability;
- repeated experience validation requires distinct supporting evidence; and
- policy promotion requires non-empty replay/shadow evaluation.

These are policy-integrity constraints, not new execution authority.

## Dependency coherence

This profile intentionally pins the same chronology-safe Agent Kernel revision
used by the pinned `meta-controller`. Controller plugin read projections select
latest decisions/results by durable event chronology rather than relying on a
store's list ordering. This is required for a reopened episode to observe its
actual latest `REOPEN` decision instead of an older diagnosis/closure stage.

The lockfile is refreshed whenever these git pins change; CI is expected to run
against the locked dependency graph.
