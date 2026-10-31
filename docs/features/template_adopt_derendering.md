# 3-Way Template De-Rendering: Mathematical Model & Symbolic Algorithms

> [!NOTE]
> This document specifies the formal mathematical model, orthogonal classification calculus, and symbolic reasoning algorithms behind Drift's **3-Way Template De-Rendering Engine (`T <-> R <-> H`)**. For domain dataclasses, module boundaries, and pipeline implementation details, see the companion specification **[3-Way Template De-Rendering: Domain Models & Pipeline Architecture](template_adopt_architecture.md)**.

* **Companion Implementation Architecture**: [`docs/features/template_adopt_architecture.md`](template_adopt_architecture.md)
* **Bidirectional Drift Adoption Overview**: [`docs/features/drift_adoption_and_fcd.md`](drift_adoption_and_fcd.md)
* **Core Implementation Modules**: [`src/drift/adopt/adopt_fission.py`](../../src/drift/adopt/adopt_fission.py), [`src/drift/adopt/adopt_derender.py`](../../src/drift/adopt/adopt_derender.py), [`src/drift/adopt/adopt_patch.py`](../../src/drift/adopt/adopt_patch.py), [`src/drift/models/adopt_models.py`](../../src/drift/models/adopt_models.py)

---

## 1. Design Motivation & Problem Formulation

### 1.1 One-Way Template Compilation vs. Bidirectional Adoption
In Drift's 4-tier architecture, a declarative source template $T$ (in `src/<pkg>/`) is compiled by a template engine (such as `envsubst`, Jinja2, Mako, or an arbitrary user command) into a rendered baseline $R$ (in `render/<pkg>/`), deployed to the host, and—after live runtime modifications occur on the host—pulled back via `reverse-sync` into $H$ (in `install/<pkg>/`):

$$T \xrightarrow{\text{Render (Compile)}} R \xrightarrow{\text{Host Drift (Runtime Edit)}} H$$

When running `drift adopt` to reconcile host modifications $H$ back into the source template $T$, Drift treats the template engine as a **black-box function** without coupling to any engine-specific AST parser. Directly applying a naive 2-way diff $\text{diff}(R, H)$ onto $T$ fails due to two fundamental failure modes:
1. **False-Positive Context Conflicts**: If a user or application modifies a static line or inserts a new setting in $H$, but the surrounding 3-line unified diff context happens to contain a rendered template variable (e.g., `"key": "sk-123"` in $R$ vs. `"key": "${API_KEY}"` in $T$), standard patch tools reject the hunk due to context mismatch.
2. **Silent Variable Reference Destruction**: If a user modifies a value that was originally produced by a template variable (e.g., changing `v4` to `v4b`), naively overwriting the line in $T$ replaces `{{ value4 }}` with the literal `v4b`, silently destroying the template variable binding without warning.

To resolve both failure modes simultaneously, the de-rendering engine must satisfy two strict objectives:
* **Maximize Clean Automatic De-Rendering (Zero False Conflicts)**: Every host edit that preserves template variable bindings and control-flow integrity—including untouched template lines, static line edits, sub-line edits to the static skeleton of a templated line, complete line/block deletions, and insertions at well-defined outer boundaries—must automatically restore template expressions and update $T$ cleanly.
* **Strictly Guard Template Variables & Control Flow (Minimal 3-Way `diff3` Conflicts)**: Any host edit that alters a rendered variable's value, touches a variable's token boundary ambiguously, or slices into the interior of an irreducible multi-line template block must refuse silent overwrite and emit a minimal, exact 3-way `diff3` conflict block comparing $T$ (Template), $R$ (Rendered Baseline), and $H$ (Host Drift).

---

### 1.2 Why This Problem Is Mathematically Hard (Information Collapse)

Forward rendering $R = \text{Render}(T, \text{Env})$ is a **many-to-one, lossy compilation process**:
* **Symbolic Erasure**: `{{ value4 }}` compiles to `"v4"`, leaving zero lexical markers in $R$ indicating which characters originated from a variable slot versus static text.
* **Control-Flow Flattening**: Inactive conditional branches (`{% if false %}...{% endif %}`) evaporate into $0$ lines in $R$ (`TR_DELETE`), while loops (`{% for x in items %}`) unroll into multiple repeated lines in $R$ (`TR_BLOCK_EDIT`).

Reconciling $(T, R, H)$ into an updated template $T'$ requires computing a generalized **Putback (inverse projection)** of a non-invertible function:

$$\text{Putback}(T, R, H) \longrightarrow T'$$

Any imprecision in **coordinate alignment**, **hunk granularity**, or **boundary ambiguity detection**—even by a single line or token boundary—will either silently overwrite a user's template variable or entangle untouched lines inside false conflicts.

---

### 1.3 Prior Art in Academia & Industry

#### 1. Academic Foundations
This problem connects to two decades of programming languages research (POPL, ICFP, PLDI, UIST):

* **Bidirectional Transformations & Asymmetric Lenses (Pierce et al., POPL 2005–2012)**:
  * **Mathematical Structure**: An asymmetric lens connects a rich **Concrete Source $S$** and a lossy **Abstract View $V$** via three operators:
    * $\text{get}: S \to V$ (forward projection that discards hidden source structure such as comments, formatting, or variable bindings);
    * $\text{put}: V' \times S \to S'$ (backward putback that weaves an updated view $V'$ back into the original source $S$ while restoring hidden source metadata);
    * $\text{create}: V \to S$ (constructs a default source representation for purely new view fragments).
    Well-behaved lenses obey the **GetPut** law ($\text{put}(\text{get}(s), s) = s$) and the **PutGet** law ($\text{get}(\text{put}(v', s)) = v'$).
  * **Boomerang & Matching Lenses (POPL 2010)**:
    In *Boomerang* (a functional language for string lenses built from regex combinators), Pierce's team discovered that naive positional alignment (pairing line $i$ of $S$ with line $i$ of $V'$) suffers catastrophic domino misalignment whenever a line is inserted or deleted near the top of a file. They introduced **Matching Lenses**, which extract a structural **Key / Skeleton** for each chunk, solve an optimal alignment between old source chunks and new view chunks first, and then dispatch matched pairs to local $\text{put}$, unmatched view chunks to $\text{create}$, and unmatched source chunks to deletion.
    * *Isomorphism with Drift's Fission Engine*: Drift's intra-hunk fission—extracting word-level skeletons, solving a monotonic 2D dynamic program, and running second-pass sentinel alignment to separate `SINGLE_EDIT` (local $\text{put}$), `INSERT` ($\text{create}$), and `DELETE` (drop)—is algebraically isomorphic to Matching Lenses, but operates **post-hoc on arbitrary text without requiring a pre-declared bijective regex grammar**.
  * **Augeas (RedHat Linux Configuration Engine)**:
    Developed by David Lutterkort based on Lens theory, *Augeas* parses static `/etc/` files into an Abstract Tree and a Concrete Skeleton Tree so edits preserve whitespace and comments. However, Augeas requires format-specific `.aug` grammar definitions and only maps a **single static file $\leftrightarrow$ in-memory tree**; it has no concept of a 3-way $(T \to R \to H)$ template compilation pipeline or 3-way conflict generation.
* **Bidirectional Template Evaluation (Hu et al., Chugh et al. *Sketch-n-Sketch*)**:
  Academic bidirectional template evaluators rely on **white-box provenance instrumentation**—modifying a custom template interpreter to attach hidden AST node IDs to every output character during rendering. Because Drift supports arbitrary external CLI renderers (`envsubst`, `jinja2`, `mustache`, custom shell/Python pipelines), white-box byte tagging is impossible; Drift must reconstruct the exact variable-vs-skeleton topology purely from the three texts $(T, R, H)$.

#### 2. Industry Configuration Managers
* **Ansible / SaltStack**: Strictly one-way push; runtime host edits are overwritten on the next run.
* **GNU Stow / YADM**: Manage raw symlinks or Git worktrees without a template compilation stage.
* **Chezmoi**:
  * Running `chezmoi re-add` on a file rendered from a `.tmpl` template is rejected with an error because Chezmoi cannot invert template rendering.
  * Running `chezmoi merge` drops the raw template $T$ and the rendered host file $H$ directly into a 2-way `vimdiff`, causing every untouched `{{ .var }}` in the file to light up as a false diff that the user must manually inspect line by line.

---

### 1.4 Flaws of Coarse Hunk-Level Models ("Dual Coalescing" & Contagion)

Consider a 5-line configuration template $T$:

```ini
# Template (T)
variable1 = {{ value1 }}
variable2 = {{ value2 }}
variable3 = {{ value3 }}
variable4 = {{ value4 }}
variable5 = {{ value5 }}
```

Rendered into baseline $R$:

```ini
# Rendered Baseline (R)
variable1 = v1
variable2 = v2
variable3 = v3
variable4 = v4
variable5 = v5
```

Suppose on the host ($H$), lines 4 and 5 are modified and a new line 6 is appended:

```ini
# Drifted Host (H)
variable1 = v1
variable2 = v2
variable3 = v3
variable4 = v4b
variable5 = v5b
variable6 = v6b
```

Feeding standard line-level `difflib.SequenceMatcher` opcodes directly into a hunk classifier causes **dual coarse coalescing**:
1. **First-Layer Coalescing ($T \to R$)**: Although `variable1..5` are 5 independent single-line variable substitutions, they are contiguous and none of the lines in $T$ equal $R$ verbatim. Line-level `SequenceMatcher` merges all 5 lines into one monolithic `replace` block $T[0:5] \leftrightarrow R[0:5]$.
2. **Second-Layer Coalescing ($R \to H$)**: Because lines 4–5 are edited and line 6 is inserted immediately after line 5 with no intervening `equal` line, line-level `SequenceMatcher` merges them into one `replace` block $R[3:5] \to H[3:6]$.
3. **Conflict Contagion**: A classifier that starts from the coarse $T[0:5] \leftrightarrow R[0:5]$ hunk sees that $R[3:5]$ partially overlaps $R[0:5]$, marking the entire 5-line block as a conflict. As a result, untouched lines 1–3 (`variable1..3`) are falsely dragged into the conflict block, and the clean insertion on line 6 (`variable6 = v6b`) is trapped inside the conflict markers of lines 4–5.

---

### 1.5 Four Pillars of Drift's Orthogonal Model

1. **The $R$-Axis Bridge**:
   $T$ and $H$ share no direct coordinate space; the rendered baseline $R$ is the unique shared metric space connecting $T \leftrightarrow R$ and $R \leftrightarrow H$. All interval projections, boundary splits, and conflict envelope closures are performed on $R$-axis line intervals and point coordinates.
2. **Intra-Hunk Fission Before Classification**:
   Every coarse `"replace"` block in both $T \to R$ and $R \to H$ is decomposed via **Slot-Normalized Word-Skeleton Matching $\to$ Bidirectional Signature Uniqueness Filtering $\to$ 2D Monotonic DP $\to$ Second-Pass Sentinel `SequenceMatcher`** into minimal atomic units (`EQUAL`, `SINGLE_EDIT`, `BLOCK_EDIT`, `INSERT`, `DELETE`).
3. **Orthogonal $(T \to R) \times (R \to H)$ Classification**:
   Each atomic $R \to H$ edit is projected onto the $R$-axis against atomic $T \to R$ units using a complete 5×5 orthogonal decision matrix (`Cases 0.1–4.2`).
4. **Pure-Python Two-Step Patching (`T -> T_clean -> T_merged`)**:
   * **Step 1 ($T \to T_{\text{clean}}$)**: Applies all conflict-free de-rendered hunks directly to $T$ in a single gap-interleaving pass while leaving conflicting regions untouched as original $T$ lines.
   * **Step 2 ($T_{\text{clean}} \to T_{\text{merged}}$)**: Splices 3-way `diff3` conflict clusters onto their shifted coordinates $[t_{\text{clean\_start}}, t_{\text{clean\_end}})$ in $T_{\text{clean}}$, ensuring adjacent clean edits (like `variable6 = v6b`) sit cleanly outside the `<<<<<<< ... >>>>>>>` markers.

---

## 2. Formal Coordinate System & Atomic Units

### 2.1 Half-Open Interval & Point Coordinate Notation
* For any line sequence $X$, the half-open interval $[x_1, x_2)$ ($0 \le x_1 \le x_2 \le |X|$) denotes the slice $X[x_1:x_2]$ of length $x_2 - x_1$.
* When $x_1 = x_2$, the degenerate zero-length interval $[x_1, x_1)$ denotes the **inter-line boundary point coordinate** immediately after line $x_1 - 1$ and immediately before line $x_1$.

---

### 2.2 The 5 Atomic $T \to R$ Alignment Unit Types ($\mathcal{U}_{T \to R}$)

Each unit $u = (\text{type}_u, [t_1, t_2), [r_1, r_2)) \in \mathcal{U}_{T \to R}$ has template span $M = t_2 - t_1$ and rendered span $N = r_2 - r_1$:

| Unit Type | Span ($M \leftrightarrow N$) | Mathematical Definition | Internal Structure |
| :--- | :--- | :--- | :--- |
| **`TR_EQUAL`** | $1 \leftrightarrow 1$ | $T[t_1] == R[r_1]$ (static unchanged line) | Entire line is static skeleton |
| **`TR_SINGLE_EDIT`** | $1 \leftrightarrow 1$ | $T[t_1] \neq R[r_1]$ with valid shared skeleton | Partitioned into static skeleton & closed variable slots |
| **`TR_BLOCK_EDIT`** | $M \leftrightarrow N$ ($M,N \ge 1$) | Irreducible block ($1 \to N$, $M \to 1$, $M \to N$, or $1 \to 1$ without skeleton) | Treated as an indivisible black box |
| **`TR_INSERT`** | $0 \leftrightarrow N$ ($N \ge 1$) | Pure template generation anchored at point $[t_1, t_1)$ in $T$ | Indivisible black box |
| **`TR_DELETE`** | $M \leftrightarrow 0$ ($M \ge 1$) | Empty-rendering template block collapsed to point $[r_1, r_1)$ in $R$ | Indivisible black box |

* **Partition Completeness Invariant**: Ordered units $u \in \mathcal{U}_{T \to R}$ form a contiguous, non-overlapping partition of $[0, |T|)$ on the $T$-axis and $[0, |R|)$ on the $R$-axis, with zero-length `TR_DELETE` units positioned at inter-line boundary points $r_1 \in [0, |R|]$.

---

### 2.3 The 5 Atomic $R \to H$ Host Edit Types ($\mathcal{E}_{R \to H}$)

Each unit $e = (\text{type}_e, [d_1, d_2), [h_1, h_2)) \in \mathcal{E}_{R \to H}$ has rendered span $P = d_2 - d_1$ and host span $Q = h_2 - h_1$:

| Edit Type | Span ($P \leftrightarrow Q$) | Mathematical Definition |
| :--- | :--- | :--- |
| **`RH_EQUAL`** | $1 \leftrightarrow 1$ | $R[d_1] == H[h_1]$ (untouched by host) |
| **`RH_SINGLE_EDIT`** | $1 \leftrightarrow 1$ | $R[d_1] \neq H[h_1]$ with shared line skeleton (refined into `SINGLE_SKEL_EDIT` if clean) |
| **`RH_BLOCK_EDIT`** | $P \leftrightarrow Q$ ($P,Q \ge 1$) | Irreducible multi-line or non-skeleton replacement on host |
| **`RH_INSERT`** | $0 \leftrightarrow Q$ ($Q \ge 1$) | Pure host insertion at $R$-axis point coordinate $[d_1, d_1)$ |
| **`RH_DELETE`** | $P \leftrightarrow 0$ ($P \ge 1$) | Pure host deletion of $R[d_1:d_2]$ |

* **Boundary Normalization of `RH_DELETE` (`split_rh_deletes_at_tr_boundaries`)**:
  Any `RH_DELETE` spanning $[d_1, d_2)$ that crosses interior start boundaries of positive-length $\mathcal{U}_{T \to R}$ units is split at those exact boundary points before classification. Thus every resulting `RH_DELETE` either **100% covers** a positive-length $T \to R$ unit or falls **strictly inside** a single multi-line $T \to R$ unit, never straddling a boundary half-covered.

---

## 3. Orthogonal $(T \to R) \times (R \to H)$ Classification Matrix

Projecting each $e \in \mathcal{E}_{R \to H}$ onto $\mathcal{U}_{T \to R}$ along the shared $R$-axis yields 18 exhaustive, mutually exclusive cases across 5 rows (`0.x`–`4.x`):

| Case | $R \to H$ Edit | Overlapped $T \to R$ Unit | Geometric / Token Condition | Status | Stage | Resolution Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **0.1** | `RH_EQUAL` | `TR_EQUAL` / `TR_SINGLE_EDIT` | Single line unchanged ($R[d] == H[h]$) | **Clean** | Context | Keep $T[t_1:t_2]$ verbatim (sync EOF newline if at EOF) |
| **0.2** | `RH_EQUAL` | `TR_BLOCK_EDIT` / `TR_INSERT` | **All** lines in block are `RH_EQUAL` | **Clean** | Context | Keep block $T[t_1:t_2]$ verbatim |
| **0.3** | `RH_EQUAL` | `TR_BLOCK_EDIT` / `TR_INSERT` | **Some** lines in block are `RH_EQUAL`, others edited | **Conflict** | Step 2 | Absorbed into parent block's `ConflictCluster` |
| **1.1** | `RH_SINGLE_EDIT` | `TR_EQUAL` | Static line modified on host | **Clean** | Step 1 | Replace $T[t_1:t_2] \to H[h_1:h_2]$ |
| **1.2** | `RH_SINGLE_EDIT` | `TR_SINGLE_EDIT` | **Skeleton edit** (`RH` edits disjoint from variable slots) | **Clean** | Step 1 | Upgrade to `SINGLE_SKEL_EDIT`; replace $T[t_1:t_2] \to H'$ |
| **1.3** | `RH_SINGLE_EDIT` | `TR_SINGLE_EDIT` | **Variable edit** (`RH` edit touches variable closed interval) | **Conflict** | Step 2 | 1-line `diff3`: $T[t_1:t_2] \;\|\|\|\; R[r_1:r_2] \;===\; H[h_1:h_2]$ |
| **1.4** | `RH_SINGLE_EDIT` | `TR_BLOCK_EDIT` / `TR_INSERT` | Single-line host edit inside multi-line template block | **Conflict** | Step 2 | Escalate to full block $T[t_1:t_2]$ `ConflictCluster` |
| **2.1** | `RH_DELETE` | `TR_EQUAL` / `TR_SINGLE_EDIT` | Full deletion of single line ($d_1 \le r_1 < r_2 \le d_2$) | **Clean** | Step 1 | Delete $T[t_1:t_2]$ from $T$ |
| **2.2** | `RH_DELETE` | `TR_BLOCK_EDIT` | **Full deletion** of entire block ($d_1 \le r_1 \land r_2 \le d_2$) | **Clean** | Step 1 | Delete entire block $T[t_1:t_2]$ from $T$ |
| **2.3** | `RH_DELETE` | `TR_BLOCK_EDIT` | **Partial deletion** inside multi-line block | **Conflict** | Step 2 | Escalate to full block $T[t_1:t_2]$ `ConflictCluster` |
| **2.4** | `RH_DELETE` | `TR_INSERT` ($0 \to N$) | Full or partial deletion of template-generated block | **Conflict** | Step 2 | $M=0$ in $T$ (no source lines to delete); emit `diff3` |
| **2.5a** | `RH_DELETE` | `TR_DELETE` ($M \to 0$) | One-sided adjacent delete ($d_1 = r_1$ or $d_2 = r_1$) | **Clean** | Context | Delete adjacent static line only; keep $T[t_1:t_2]$ intact |
| **2.5b** | `RH_DELETE` | `TR_DELETE` ($M \to 0$) | Strictly enclosed delete ($d_1 < r_1 < d_2$) | **Conflict** | Step 2 | Ambiguous whether `TR_DELETE` should be kept or deleted |
| **3.1** | `RH_INSERT` | Outer boundary of non-insert unit | At boundary $d_1$ with no `TR_DELETE` at $d_1$ | **Clean** | Step 1 | Unique bijection $d_1 \mapsto t^*$; insert $H[h_1:h_2]$ at $T[t^*:t^*]$ |
| **3.2** | `RH_INSERT` | Interior of `TR_BLOCK_EDIT` | Strictly inside block ($r_1 < d_1 < r_2$) | **Conflict** | Step 2 | No corresponding line boundary in $T$; escalate to block |
| **3.3** | `RH_INSERT` | Touching `TR_DELETE` ($M \to 0$) | Insertion point coincides with `TR_DELETE` ($r_1 = r_2 = d_1$) | **Conflict** | Step 2 | Point $d_1$ maps to non-empty interval $[t_1, t_2]$ (loss of anchor) |
| **3.4** | `RH_INSERT` | Interior of `TR_INSERT` ($0 \to N$) | Strictly inside generated block ($r_1 < d_1 < r_2$) | **Conflict** | Step 2 | Slices into generated output; escalate to `TR_INSERT` block |
| **3.5** | `RH_INSERT` | Outer boundary of `TR_INSERT` | At $d_1 = r_1$ or $d_1 = r_2$ with no `TR_DELETE` at $d_1$ | **Clean** | Step 1 | Anchors uniquely to $t^* = t_1 = t_2$ outside generated block |
| **4.1** | `RH_BLOCK_EDIT` | Pure `TR_EQUAL` sequence | $[d_1, d_2)$ overlaps only `TR_EQUAL` and no `TR_DELETE` | **Clean** | Step 1 | Replace static slice $T[t_1:t_2] \to H[h_1:h_2]$ |
| **4.2** | `RH_BLOCK_EDIT` | Any non-`TR_EQUAL` unit | Overlaps `TR_SINGLE_EDIT` / `BLOCK_EDIT` / `INSERT` / `DELETE` | **Conflict** | Step 2 | Merge overlapped units into envelope `ConflictCluster` |

---

### 3.1 Deep-Dive Proofs of Critical Cross-Cases

#### 1. Bijective Point-Anchor Theorem for `RH_INSERT` (`Cases 3.1 & 3.5` vs. `Cases 3.2, 3.3 & 3.4`)
Suppose the host inserts lines $H[h_1:h_2]$ at point coordinate $d \in [0, |R|]$. A conflict-free insertion into $T$ exists if and only if **$d \in R$ maps to a unique inter-line point coordinate $t^* \in T$ without slicing the interior of any indivisible template unit**:
* **Case A — Interior of a Multi-Line Block (`Cases 3.2 & 3.4`, $r_1 < d < r_2$)**:
  Occurs only inside a `TR_BLOCK_EDIT` or `TR_INSERT` with $N = r_2 - r_1 \ge 2$. Because the block cannot be decomposed line-by-line between $T$ and $R$, point $d$ has no corresponding inter-line boundary inside $T[t_1:t_2]$. Hence it is a **conflict**.
* **Case B — Coinciding with a Zero-Length `TR_DELETE` (`Case 3.3`, $r_1 = r_2 = d$)**:
  A `TR_DELETE` ($M \to 0, M \ge 1$) occupies a positive interval $[t_1, t_2)$ ($t_1 < t_2$) in $T$ that collapsed to the single point $d$ in $R$. Inverting point $d$ back to $T$ yields the set-valued interval $[t_1, t_2]$—making it impossible to know whether the new host lines belong before line $t_1$ (outside an `{% if %}` block) or after line $t_2$. This **one-to-many loss of anchor** is necessarily a **conflict**.
* **Case C — Outer Boundary of Positive-Length Units Without `TR_DELETE` (`Cases 3.1 & 3.5`)**:
  When $\neg(r_1 < d < r_2)$ for all positive-length $T \to R$ units and no `TR_DELETE` exists at $d$:
  * If $d$ lies between two non-insert units $u_{\text{prev}}([t_{k,1}, t^*) \leftrightarrow [r_{k,1}, d))$ and $u_{\text{next}}([t^*, t_{k+1,2}) \leftrightarrow [d, r_{k+1,2}))$ (**Case 3.1**), $d \mapsto t^*$ is a strict bijection.
  * If $d$ lies at the left boundary $d = r_1$ or right boundary $d = r_2$ of a `TR_INSERT` ($[t^*, t^*) \leftrightarrow [r_1, r_2)$) (**Case 3.5**), inserting outside the generated block does not disturb its interior, and the natural lexicographical order of $R$-intervals $(r_1, r_1) < (r_1, r_2) < (r_2, r_2)$ unambiguously orders a left-boundary insertion before the block and a right-boundary insertion after the block in $T_{\text{clean}}$.

#### 2. Why `RH_BLOCK_EDIT` Over `TR_SINGLE_EDIT` (`Case 4.2`) Must Conflict
If $T[t]$ is `variable4 = {{ value4 }}` and $R[d]$ is `variable4 = v4`, rewriting that line to `variable4 = v4b` conflicts under **Case 1.3** to protect `{{ value4 }}`. If the user rewrites that line into two completely unrelated lines `foo = 1\nbar = 2` (`RH_BLOCK_EDIT` with no shared skeleton), allowing it to silently overwrite `T[t]` would create a paradox where a small value tweak is guarded as a conflict while a total rewrite silently destroys `{{ value4 }}`. Hence any `RH_BLOCK_EDIT` touching a non-`TR_EQUAL` unit must conflict (**Case 4.2**).

#### 3. One-Sided vs. Strictly Enclosed Deletion Around `TR_DELETE` (`Case 2.5a` vs. `Case 2.5b`)
A `TR_DELETE` ($M \to 0$ at $r_1$) represents $M$ lines in $T$ (e.g., an inactive OS branch `{% if os == "macos" %}...{% endif %}` or a template comment `{# ... #}`) that rendered to $0$ lines on the current host:
* **One-Sided Adjacent Deletion (`Case 2.5a`, $d_1 = r_1$ or $d_2 = r_1$)**: The host deleted lines only above $r_1$ or only below $r_1$. The deletion does not cross $r_1$, so $T[t_1:t_2]$ remains untouched (**Clean**).
* **Strictly Enclosed Deletion (`Case 2.5b`, $d_1 < r_1 < d_2$)**: The host deleted a contiguous region spanning both sides of $r_1$ in a single wipe. Without an AST, it is fundamentally ambiguous whether $T[t_1:t_2]$ is an unrelated sibling block that must be preserved (such as a macOS block when surrounding Linux lines are deleted) or an inner comment/clause that the user intended to delete along with the surrounding section. Therefore, Step 1 cleanly deletes the surrounding positive-length lines while Step 2 flags the enclosed `TR_DELETE` as a `diff3` conflict (`<<<<<<< T[t1:t2] ||||||| ======= >>>>>>>`) so the user explicitly decides whether to keep or delete those $M$ template lines.

---

## 4. Core Symbolic Algorithms

### 4.1 Lossless Word-Level Tokenization & Slot-Normalized Skeleton Similarity

#### 1. Why Word-Level Tokenization Instead of Character-Level Diffing?
In character-level `SequenceMatcher`, `"variable1 = v1"` and `"variable2 = v2"` share the 8-character prefix `"variable"` plus `" = v"`, yielding an **86% (12/14)** character similarity; deleting line 1 would falsely align `variable1` with `variable2`. Similarly, character-level diffing treats `"v4"` and `"v4b"` as sharing `"v4"` with a 1-character suffix `"b"`, whereas lexically `v4` and `v4b` are distinct identifier tokens.

Following Neovim's character class partitioning (`mbyte.c` / `iskeyword`), [`tokenize_line`](../../src/drift/adopt/adopt_fission.py) strips the trailing line ending (`\r\n` or `\n`) and partitions the line body with the regex `_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")` into three mutually exclusive token kinds:
1. **`TokenKind.WORD` (`\w+`)**: Contiguous Unicode letters, digits, and underscores (e.g., `variable1`, `v4`, `v4b`, `_private`, `变量`). Because `"variable1" != "variable2"` and `"v4" != "v4b"`, partial identifier prefix matches are impossible.
2. **`TokenKind.PUNCT` (`[^\w\s]`)**: Single punctuation/operator character (e.g., `=`, `{`, `}`, `;`, `"`). Splitting punctuation into single characters ensures that in `variable4 = {{ value4 }};`, the closing `}}` belongs to the template tag while the trailing `;` independently matches the `;` in `variable4 = v4;` as static skeleton.
3. **`TokenKind.SPACE` (`\s+`)**: Contiguous horizontal whitespace.

Because $\mathcal{W} \cup \mathcal{P} \cup \mathcal{S} = \Sigma$, tokenization is **100% lossless**: `"".join(tok.tokens) + tok.line_ending == tok.raw_line`. Each `TokenizedLine` also precomputes `word_set: FrozenSet[str]` containing all `WORD` tokens for $O(1)$ disjointness pruning.

#### 2. Space-Bridge Coalescing (`_coalesce_space_bridged_opcodes`)
When a template tag containing internal spaces (such as `{{ value1 }}` or `{{ app name }}`) renders to a multi-word value (such as `hello world`), token-level `SequenceMatcher` aligns the internal space inside `{{ value1 }}` with the space between `hello` and `world`, splitting one logical variable slot into two replacement spans (`"{{" + "value1" -> "hello"` and `"}}" -> "world"`).

[`_coalesce_space_bridged_opcodes`](../../src/drift/adopt/adopt_fission.py) folds over the raw `_TokenOpcode` sequence and merges any non-equal opcodes that are adjacent or separated solely by a whitespace-only `"equal"` opcode (`op.is_space_only_equal`), guaranteeing that each template expression is bounded and counted as a single contiguous slot.

#### 3. Structural Gate & Slot-Normalized Score (`_compute_skeleton_score`)
Using raw character length in the similarity denominator would penalize long template expressions (`key = "{{ very_long_expression }}"`) or long rendered secrets (`key = "sk-proj-100-chars..."`). Instead, after space-bridge coalescing:
* **Matched Skeleton Counts**: Across all `"equal"` opcodes, count matched `WORD` tokens ($N_{\text{word}}$) and matched `PUNCT` tokens ($N_{\text{punct}}$, ignoring `SPACE`).
* **Structural Gate**: Requires $N_{\text{word}} \ge 1$ when either line contains words (`both_lack_words=False`), or $N_{\text{punct}} \ge 2$ when both lines contain zero words (`both_lack_words=True`). If the gate fails, the score is $0.0$.
* **Slot Count ($N_{\text{slots}}$)**: Across all non-equal opcodes, count spans containing at least one non-whitespace token (`op.has_non_space_tokens`). Whitespace-only edits contribute $0$ slots.
* **Slot-Normalized Formula & Threshold (`_SIMILARITY_THRESHOLD = 0.5`)**:
  $$E_{\text{skel}} = 1.0 \cdot N_{\text{word}} + 0.25 \cdot N_{\text{punct}}, \qquad S = \frac{E_{\text{skel}}}{E_{\text{skel}} + 1.0 \cdot N_{\text{slots}}} \ge 0.5$$
  The threshold $S \ge 0.5$ is the exact balance point $E_{\text{skel}} \ge N_{\text{slots}}$ (static skeleton weight must be at least as large as the number of variable/edit slots):
  * `return {{ val }}` $\leftrightarrow$ `return 0`: $E_{\text{skel}} = 1.0,\; N_{\text{slots}} = 1 \implies S = 0.500$ (**Valid**).
  * `variable1 = {{ value1 }}` $\leftrightarrow$ `variable1 = v1`: $E_{\text{skel}} = 1.25,\; N_{\text{slots}} = 1 \implies S = \frac{1.25}{2.25} \approx 0.556$ (**Valid**, invariant to expression or secret length).
  * `export A = {{ a }}` $\leftrightarrow$ `export B = 1`: $E_{\text{skel}} = 1.25,\; N_{\text{slots}} = 2 \implies S = \frac{1.25}{3.25} \approx 0.385 < 0.5$ (**Rejected**).

---

### 4.2 Bidirectional Signature Uniqueness, 2D DP & Sentinel Fission

For eachValid `SkeletonMatch`, `op.signature_tokens` emits the tuple of matched non-space tokens interspersed with `"\x00"` slot markers (e.g., `("server", "=", "\x00")`).

#### 1. Four Bidirectional Signature Uniqueness Rules (`_filter_unique_skeleton_matrix`)
Given the $M \times N$ pairwise `SkeletonMatch` matrix for a `"replace"` block `a_lines[0..M-1] <-> b_lines[0..N-1]`, [`_filter_unique_skeleton_matrix`](../../src/drift/adopt/adopt_fission.py) computes per-row signature counts `row_sig_counts` and per-column signature counts `col_sig_counts` (via matrix transpose `zip(*raw_matrix)`):
1. **Candidate Scope**: Only valid cells (`cell.is_valid == True`, i.e., passed gate and $S \ge 0.5$) are counted in `row_sig_counts[i]` and `col_sig_counts[j]`.
2. **Row Uniqueness (`row_sig_counts[i][sig] == 1` — `1 -> N` Loop-Unrolling Guard)**:
   In a `{% for %}` loop, a single template line `T[i]` (`server = {{ host }}`) matches multiple unrolled rendered lines `R[0], R[1], R[2]` (`server = node1..3`) across row `i` with identical signature `("server", "=", "\x00")` (`row_sig_counts[i][sig] == 3`, while each column has `col_sig_counts[j][sig] == 1`). Row uniqueness invalidates all 3 cells so the loop body stays intact as a `BLOCK_EDIT`.
3. **Column Uniqueness (`col_sig_counts[j][sig] == 1` — `M -> 1` Branch-Ambiguity Guard)**:
   In an `{% if %}` / `{% else %}` block, two template branches `T[1]` (`server = {{ prod }}`) and `T[3]` (`server = {{ dev }}`) both match the single rendered output line `R[0]` (`server = 10.0.0.1`) down column `0` (`col_sig_counts[0][sig] == 2`, while each row has `row_sig_counts[i][sig] == 1`). Column uniqueness invalidates both cells so `R[0]` never arbitrarily anchors to one branch and deletes the other.
4. **Per-Signature Grouping Preserves Shared-Prefix Diagonals**:
   Counts are grouped by exact `cell.signature` tuple rather than total valid matches in a row/column. If `set_header X_Real_IP {{ ip }};` has a specific diagonal match `(0, 0)` with signature `("set_header", "X_Real_IP", "\x00", ";")` (count `1`) alongside weaker off-diagonal matches `(0, 1), (0, 2)` against other `set_header` lines with generic signature `("set_header", "\x00", ";")` (count `2`), only the repeated generic signature is invalidated while the unique diagonal match `(0, 0)` survives.

#### 2. Monotonic 2D Dynamic Programming (`find_monotonic_line_matches`)
To guarantee strictly order-preserving 1-to-1 line pairs ($i_1 < i_2 \iff j_1 < j_2$), [`find_monotonic_line_matches`](../../src/drift/adopt/adopt_fission.py) solves the 2D recurrence over $(M + 1) \times (N + 1)$ `_DpCell(score, pairs)` states:

$$\text{dp}[i][j] = \max \begin{cases} \text{dp}[i-1][j] \\ \text{dp}[i][j-1] \\ \text{dp}[i-1][j-1] + \text{matrix}[i-1][j-1].\text{score} & (\text{if } \text{matrix}[i-1][j-1].\text{is\_valid}) \end{cases}$$

#### 3. Second-Pass Sentinel Alignment (`_fission_multiline_replace_block`)
For each matched relative index pair $(i_k, j_k)$ returned by the 2D DP, both `sub_src[i_k]` and `sub_dst[j_k]` are replaced with the unique sentinel string `f"\x00__DRIFT_MATCH_{k}__\n"`. Running a second-pass `SequenceMatcher(None, sub_src, sub_dst, autojunk=False)` automatically decomposes the block:
* `"equal"` spans on sentinels (or identical raw lines) expand into individual $1 \leftrightarrow 1$ `SINGLE_EDIT` (or `EQUAL`) units;
* Unmatched intervals between sentinels partition into minimal `INSERT`, `DELETE`, or `BLOCK_EDIT` units.

---

### 4.3 Single-Line (`1 <-> 1 <-> 1`) Closed Token-Interval Calculus

When line $r$ is aligned as both `TR_SINGLE_EDIT` ($t_i \leftrightarrow r$) and `RH_SINGLE_EDIT` ($r \leftrightarrow h_j$) (**Cases 1.2 & 1.3**), [`try_derender_single_line`](../../src/drift/adopt/adopt_fission.py) projects both relations onto the token index axis $[0, |r.\text{tokens}|]$ of $r$:
1. **Variable Slots ($\mathcal{C}_{\text{var}}$)**: Extracted from space-bridge-coalesced `T <-> R` opcodes as `SpliceSpan(start=op.b_start, end=op.b_end, replacement=op.a_tokens)` for all `not op.is_equal`.
2. **Host Edits ($\mathcal{P}_{\text{host}}$)**: Extracted from uncoalesced `R <-> H` opcodes as `SpliceSpan(start=op.a_start, end=op.a_end, replacement=op.b_tokens)` for all `not op.is_equal`.
3. **Closed-Interval Overlap Check (`SpliceSpan.overlaps_closed`)**:
   Two intervals $[p_1, p_2]$ and $[c_1, c_2]$ touch or overlap in closed token-boundary space if and only if:
   $$\max(p_1, c_1) \le \min(p_2, c_2)$$
   * **Case 1.3 (Conflict $\to$ returns `None`)**: Any host edit that overwrites a variable token ($p_1 < c_2 \land c_1 < p_2$) **or** performs a zero-width insertion/deletion immediately adjacent to a variable slot boundary ($p_2 = c_1$ or $p_1 = c_2$, such as appending `!` to `key = 8080` $\to$ `key = 8080!` where `8080` is `{{ port }}` at $[4, 5]$ and `!` inserts at $[5, 5]$) satisfies $\max(p_1, c_1) \le \min(p_2, c_2)$ and is flagged as a conflict.
   * **Case 1.2 (Clean Disjoint Skeleton Edit $\to$ returns $H'$)**: When separated from the variable slot by at least one static skeleton token (e.g., `key = 8080; # v1` $\to$ `key = 8080; # v2`, where `;` sits at $[5, 6)$ between slot $[4, 5]$ and edit $[6, 8]$), all spans in $\mathcal{C}_{\text{var}} \cup \mathcal{P}_{\text{host}}$ are strictly disjoint. Sorting them by `(start, end)` and splicing onto `r_line.tokens` via `splice_spans` synthesizes the de-rendered template line $H'$ in a single $O(W)$ pass.

---

### 4.4 Partitioned $R$-Range & `bisect` Conflict Envelope Closure

After `_classify_orthogonal_units` tags individual conflicting units (`is_conflict=True`), [`_resolve_derender_spans`](../../src/drift/adopt/adopt_derender.py) resolves all Step 1 clean spans and Step 2 conflict envelopes by partitioning both unit streams via `partition(lambda u: u.is_positive_length, ...)`:
1. **Positive-Length Stream ($\mathcal{U}_{T \to R}^+,\; \mathcal{E}_{R \to H}^+$ where $r_{\text{start}} < r_{\text{end}}$)**:
   * **Strict-Overlap Merging (`_merge_positive_conflict_r_ranges`)**: Positive-length conflict intervals $[r_1, r_2)$ are sorted and merged **only on strict overlap** ($\text{cur\_start} < \text{prev\_end}$). Touching intervals ($\text{cur\_start} == \text{prev\_end}$, such as adjacent conflicting `variable4` and `variable5` lines) share a synchronized $(T, R, H)$ boundary at $\text{prev\_end}$ and remain separate minimal conflict clusters.
   * **Synchronized Envelope Closure (`_build_positive_conflict_spans`)**: Because an `RH_BLOCK_EDIT` can overlap a `TR_BLOCK_EDIT` that extends further on $R$, `_expand_r_range_to_pos_units` alternates $O(\log L)$ `bisect_right` / `bisect_left` queries on $\mathcal{U}_{T \to R}^+$ and $\mathcal{E}_{R \to H}^+$ until the interval $[R_{\text{start}}, R_{\text{end}})$ reaches a fixed point aligned on both streams' outer boundaries, yielding $(T[T_{\text{start}}:T_{\text{end}}], R[R_{\text{start}}:R_{\text{end}}], H[H_{\text{start}}:H_{\text{end}}])$.
2. **Zero-Length Stream ($\mathcal{U}_{T \to R}^0 = \{\text{TR\_DELETE}\},\; \mathcal{E}_{R \to H}^0 = \{\text{RH\_INSERT}\}$ where $r_{\text{start}} = r_{\text{end}} = d$)**:
   * Zero-length conflicts strictly inside a positive conflict envelope ($R_{\text{start}} < d < R_{\text{end}}$) are already enclosed by the positive envelope's endpoints.
   * Every remaining zero-length conflict point $d$ (such as **Case 2.5b** or **Case 3.3**) aggregates all `TR_DELETE` and `RH_INSERT` units at $d$ via `bisect_left` / `bisect_right` into a standalone zero-length conflict span at $R[d:d]$.

---

### 4.5 Two-Step Patching & Prefix-Sum Coordinate Shifting

Once all clean and conflicting `_DerenderSpan` records are ordered along $T$:
1. **Step 1 Clean Template Synthesis (`_apply_clean_spans_to_template`)**:
   Projects all non-conflicting spans onto `SpliceSpan(t_start, t_end, replacement)` and splices them onto `t_stripped_lines` via `splice_spans` in $O(L)$ time to produce $T_{\text{clean}}$. Conflicting intervals in $T$ are absent from the clean span list, so they fall into the unchanged gaps between clean spans and remain preserved verbatim in $T_{\text{clean}}$.
2. **Step 2 Prefix-Sum Coordinate Shifting (`_anchor_conflict_clusters_to_t_clean`)**:
   Each clean span shifts downstream line indices in $T_{\text{clean}}$ by $\Delta_s = |\text{replacement}_s| - (t_{\text{end},s} - t_{\text{start},s})$, whereas each conflict span has $\Delta_s = 0$ during Step 1. Computing prefix sums via `itertools.accumulate(span_deltas, initial=0)` shifts every conflict span $[t_{\text{start}}, t_{\text{end}})$ in $T$ to its exact coordinates in $T_{\text{clean}}$:
   $$[t_{\text{clean\_start}}, t_{\text{clean\_end}}) = [t_{\text{start}} + \text{shift}, t_{\text{end}} + \text{shift})$$
3. **Pure-Python `diff3` Splicing (`splice_conflict_clusters_diff3`)**:
   Both preview diff generation (`build_display_unified_diff`) and Step 2 file execution (`apply_source_patch`) call `splice_conflict_clusters_diff3(t_clean_lines, conflict_clusters)` to splice `<<<<<<<` ($T$) / `|||||||` ($R$) / `=======` ($H$) / `>>>>>>>` blocks onto $T_{\text{clean}}$ in a single $O(L)$ pass with zero external subprocess dependencies.

---

## 5. Mathematical Rigor & Invariants Summary

1. **Partition Completeness**: The 5 unit types in $\mathcal{U}_{T \to R}$ and $\mathcal{E}_{R \to H}$ cover all possible `SequenceMatcher` shapes ($0 \to N$, $M \to 0$, $1 \to 1$, $M \to N$) without gaps or overlaps on $T$, $R$, or $H$.
2. **Strict Monotonicity**: Intra-hunk 2D DP enforces $i_1 < i_2 \iff j_1 < j_2$, and inter-hunk ordering preserves top-level `SequenceMatcher` topology, preventing any line crossing across $T$, $R$, and $H$.
3. **Bijective Point Anchor Criterion**: An `RH_INSERT` at point $d \in R$ conflicts if and only if $d$ lies strictly inside a multi-line unit ($r_1 < d < r_2$, **Cases 3.2 & 3.4**) or touches a zero-length `TR_DELETE` ($r_1 = r_2 = d$, **Case 3.3**). At all outer boundaries without `TR_DELETE` (**Cases 3.1 & 3.5**), the target coordinate $t^* \in T$ is unique.
4. **Two-Step Boundary Orthogonality**: Because positive conflict envelopes are closed under mutual boundary expansion on $\mathcal{U}_{T \to R}^+$ and $\mathcal{E}_{R \to H}^+$, Step 1 clean intervals and Step 2 conflict intervals are strictly disjoint on $T$, making the coordinate shift $T \to T_{\text{clean}} \to T_{\text{merged}}$ an exact order-preserving bijection.

---

## 6. Algorithmic Time & Space Complexity

Let $L = \max(|T|, |R|, |H|)$ be the maximum file line count, $W$ be the average number of tokens per line, and $M_b \times N_b$ be the dimensions of the $b$-th `"replace"` block ($\sum_b M_b \le L,\; \sum_b N_b \le L$):

| Pipeline Stage | Core Operation | Typical Time | Worst-Case Time | Space |
| :--- | :--- | :--- | :--- | :--- |
| **1. Initial Line Diff** | `SequenceMatcher(T, R)` and `(R, H)` | $O(L \log L)$ | $O(L^2)$ | $O(L)$ |
| **2.1 Line Tokenization** | `_TOKEN_RE.findall` + `word_set` extraction | $O(L \cdot W)$ | $O(L \cdot W)$ | $O(L \cdot W)$ |
| **2.2 Skeleton Matrix** | $O(1)$ `word_set.isdisjoint` prune + token diff | $O(M_b N_b + \min(M_b, N_b) W)$ | $O(M_b N_b W^2)$ | $O(M_b N_b)$ |
| **2.3 Uniqueness Filter** | Row/column `Counter` via transpose `zip(*matrix)` | $\Theta(M_b N_b)$ | $\Theta(M_b N_b)$ | $\Theta(M_b N_b)$ |
| **2.4 2D DP & 2nd Diff** | Monotonic 2D DP + unique sentinel `SequenceMatcher` | $\Theta(M_b N_b) + \Theta(M_b + N_b)$ | $\Theta(M_b N_b)$ | $\Theta(M_b N_b)$ |
| **3. Single-Line Calculus** | Closed token-interval overlap + `splice_spans` | $O(L \cdot W)$ | $O(L \cdot W^2)$ | $O(W)$ |
| **4. $R$-Axis Closure** | `partition` + `bisect_left`/`bisect_right` projection | $O(L \log L)$ | $O(L \log L)$ | $O(L)$ |
| **5. Two-Step Splicing** | `accumulate` shift + `splice_spans` ($T \to T_{\text{clean}} \to T_{\text{merged}}$) | $O(L)$ | $O(L)$ | $O(L)$ |

* **$O(1)$ Word-Set Disjointness Acceleration**: In a 50-line `variable1 .. variable50` block ($50 \times 50 = 2500$ pairs), $2450$ off-diagonal pairs have disjoint `word_set`s and are rejected in $O(1)$ time without running token-level `SequenceMatcher`, reducing token diff invocations from $M_b N_b$ to $\min(M_b, N_b)$.
* **Strict $\Theta(M_b + N_b)$ Second-Pass Sentinel Diff**: Because each sentinel `"\x00__DRIFT_MATCH_k__\n"` appears exactly once in `sub_src` and `sub_dst` in strictly monotonic order, second-pass `SequenceMatcher` experiences zero hash collisions or backtracking.
* **Practical Runtime**: For typical configuration files ($L \le 1000,\; W \le 25,\; M_b, N_b \le 50$), end-to-end pure-Python de-rendering completes in **1–5 ms** per file.
