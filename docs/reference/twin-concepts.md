# Twin Concepts and Vocabulary

**Status:** Draft, 2026-11-05. This is the conceptual reference for Axiom's twin support. Definitions are also in `glossary-axiom.toml`, which is the machine-readable source.

Axiom is an agentic platform: a harness, a data platform, and first-class support for digital twins. This page fixes the words for the third part, and ties them to the field. A domain layer may specialise a term. It may not redefine one.

## The core chain

A **physical counterpart** is observed. A **computational model** represents it. A **surrogate** stands in for an expensive model. An **estimator** keeps the model's state aligned with measurements. A **credibility** case says what it may be used for. How tightly all of this is coupled to the physical counterpart decides the **rung**.

## Rungs of integration

| Rung | Data flow | Physical effect |
|---|---|---|
| **Digital model** | Manual both ways | None |
| **Digital shadow** | Automatic physical to digital, manual back | None without a person |
| **Digital twin** | Automatic both ways | Outputs change the physical counterpart |

These are Kritzinger et al. (2018). A person acting on advice keeps a system at the shadow rung.

The National Academies (2024) define a digital twin more broadly: virtual constructs updated from a physical twin, with predictive capability, where bidirectional interaction is central. We use the Academies' framing for what a twin **is**, and Kritzinger's rungs to say **how far** a given deployment has reached. The phrase "digital twin" in a requirement always means the top rung. Use "twin system" for the whole program of work.

## Models

| Term | Meaning |
|---|---|
| **Computational model** | Any executable representation of a system. Say "model" only where it cannot be read as an LLM |
| **High-fidelity model** | The most accurate model available, used as a reference |
| **Surrogate** | A cheaper stand-in that approximates a higher-fidelity model or a system's behaviour. It is the umbrella term |
| **Data-fit surrogate** | Built by regression or interpolation of data. Neural networks and Gaussian processes are examples |
| **Reduced-order model (ROM)** | Derived from a high-fidelity model or its solution data by lowering the state dimension. Galerkin projection and dynamic mode decomposition are examples |
| **Multi-fidelity surrogate** | Uses a lower-fidelity model, with coarser resolution or less physics, in place of the expensive one |
| **Assembly** | A composite of coupled models, any of which may be surrogates |

Every ROM is a surrogate. Not every surrogate is a ROM. The three-way split follows the Dakota project's taxonomy, whose labels have varied between releases, and Peherstorfer et al. (2018) on multi-fidelity methods.

A surrogate differs from an **estimator**. A surrogate predicts. An estimator, such as a Kalman filter, corrects state from measurements. A twin system usually needs both. Predicting forward from a chosen action is a **what-if**, kept separate from the live stream.

## Credibility

| Term | Meaning |
|---|---|
| **Verification** | Is the model solved correctly? |
| **Validation** | Is it the right model, measured against reality? |
| **Uncertainty quantification** | What is the size and source of the error? |
| **VVUQ** | All three, as a continuing process rather than a one-time gate |
| **Context of use** | The specific role and decision a model supports |
| **Model risk** | The decision's dependence on the model, combined with the consequence of being wrong |
| **Credibility** | The justified trust in a model for one context of use |
| **Validation domain** | The conditions where the model was checked against reality |
| **Application domain** | The conditions where it will be used |
| **Interpolation, extrapolation** | Using a model inside, or outside, its validation domain |

Credibility is never a general property of a model, and it is set by risk (ASME V&V 40). A model is credible **for** a context of use. The National Academies treat VVUQ as continual, so it follows the physical counterpart as it changes.

**Credibility profile** is Axiom's term for the stages and evidence a domain requires.

## Lifecycle and deployment

| Term | Meaning |
|---|---|
| **Revision** | One immutable version of a model |
| **Package** | A revision with what a host needs to run it |
| **Digital thread** | The connected record of a revision's origin, data, evidence and use. Model Corral's lineage is this |
| **Promotion** | A person's recorded decision to stage a revision |
| **Placement** | A revision on a host, in a role |
| **Served** | The revision whose outputs consumers receive |
| **Challenger** | A revision receiving the same input frames, judged but not used |
| **Mirror** | Feeding a challenger the same live frames |
| **Operating configuration** | Typed variables, with uncertainty, describing the state a model must match. A control-theory reader would call this the controller's **process model** |
| **Service class** | The deadline and resolution a revision declares it meets |

## Words to avoid or qualify

| Word | Problem | Use |
|---|---|---|
| **Shadow deployment** | Common in machine learning, and collides with the digital shadow rung | Mirror |
| **Shadow** alone | Also used for supervised agent runs | Digital shadow, or `shadow record` for agents |
| **Tier** | Already means access tier, and data tier | Service class |
| **Twin** for a composed object | Formally this is a composite of models | Assembly |
| **"A twin at the shadow rung"** | Contradicts the rungs | A surrogate served at the shadow rung |
| **Model** alone | Could be an LLM | Computational model, or surrogate |

Library class names remain their authors' choice. When a library names a class `Twin`, cite it as code and use the formal term in prose.

## Mapping to standards

Where a standard has a term for the same concept, keep the mapping here rather than inventing a synonym.

| Axiom term | Field term | Source |
|---|---|---|
| Physical counterpart | Physical twin | National Academies (2024) |
| Physical counterpart | Observable manufacturing element | ISO 23247 |
| Physical counterpart | Asset | IEC 63278 |
| Package | Functional Mock-up Unit, the nearest public analogue | FMI 3.0 |
| Digital thread | Digital thread | ISO 23247-5, AIAA |

Adopting FMI as an adapter is an option worth testing: an FMU could be one more thing a worker runs. Nothing here depends on it.

## Foundations

Cite these first. Where we extend or differ from them, say so.

| Topic | Work |
|---|---|
| Origin of the digital twin | Grieves and Vickers, "Digital Twin: Mitigating Unpredictable, Undesirable Emergent Behavior in Complex Systems", in *Transdisciplinary Perspectives on Complex Systems*, Springer, 2017. DOI 10.1007/978-3-319-38756-7_4 |
| Early formal definition | Glaessgen and Stargel, "The Digital Twin Paradigm for Future NASA and U.S. Air Force Vehicles", 53rd AIAA SDM Conference, 2012 |
| Integration rungs | Kritzinger, Karner, Traar, Henjes and Sihn, "Digital Twin in manufacturing: A categorical literature review and classification", IFAC-PapersOnLine 51(11), 2018. DOI 10.1016/j.ifacol.2018.08.474 |
| Reference report | National Academies, *Foundational Research Gaps and Future Directions for Digital Twins*, 2024 |
| Probabilistic formalism | Kapteyn, Pretorius and Willcox, "A probabilistic graphical model foundation for enabling predictive digital twins at scale", *Nature Computational Science* 1(5), 2021. DOI 10.1038/s43588-021-00069-0 |
| Surveys | Tao, Zhang, Liu and Nee, "Digital Twin in Industry: State-of-the-Art", *IEEE TII* 15(4), 2019. DOI 10.1109/TII.2018.2873186. Rasheed, San and Kvamsdal, "Digital Twin: Values, Challenges and Enablers From a Modeling Perspective", *IEEE Access* 8, 2020. DOI 10.1109/ACCESS.2020.2970143 |
| Computer experiments | Sacks, Welch, Mitchell and Wynn, "Design and Analysis of Computer Experiments", *Statistical Science* 4(4), 1989 |
| Calibration | Kennedy and O'Hagan, "Bayesian calibration of computer models", *JRSS B* 63(3), 2001. DOI 10.1111/1467-9868.00294 |
| Surrogates | Forrester, Sóbester and Keane, *Engineering Design via Surrogate Modelling*, Wiley, 2008 |
| Model reduction | Benner, Gugercin and Willcox, "A Survey of Projection-Based Model Reduction Methods for Parametric Dynamical Systems", *SIAM Review* 57(4), 2015 |
| Dynamic mode decomposition | Schmid, "Dynamic mode decomposition of numerical and experimental data", *J. Fluid Mech.* 656, 2010. DOI 10.1017/S0022112010001217 |
| Multi-fidelity | Peherstorfer, Willcox and Gunzburger, "Survey of Multifidelity Methods in Uncertainty Propagation, Inference, and Optimization", *SIAM Review*, 2018 |
| State estimation | Kalman, "A New Approach to Linear Filtering and Prediction Problems", *J. Basic Engineering* 82, 1960 |
| Credibility | ASME V&V 40-2018. NASA-STD-7009A. Oberkampf and Roy, *Verification and Validation in Scientific Computing*, Cambridge, 2010 |
| Control and safety | Leveson, *Engineering a Safer World*, MIT Press, 2011 |
| Terminology standards | ISO/IEC 30173:2023. ISO 23247 series. IEC 63278-1:2023. AIAA and AIA, *Digital Twin: Definition & Value*, 2020 |
| Interchange | FMI 3.0, Modelica Association, 2022 |
| Surrogate classes | Dakota project documentation, whose class labels vary by release |

## How far each source has been checked

All bibliographic records above were confirmed. Most of the works themselves were not read in full. Before publication, an author should read each source cited for a specific claim, and in particular:

- **Wording not checked:** ISO/IEC 30173, ISO 23247, IEC 63278 and ASME V&V 40 are paywalled or partly public. Definitions here are in our own words, and no conformance is claimed.
- **Rungs:** Kritzinger's categories were confirmed through summaries, not the paper's full text.
- **Page ranges:** sources disagree on Sacks et al. (1989), so check the journal before citing pages.
