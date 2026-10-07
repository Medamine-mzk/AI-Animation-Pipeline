# Tâches différées

Problèmes identifiés, confirmés, mais **non corrigés** — reportés pour plus tard.
Chacun indique ce qui a déjà été écarté, pour ne pas refaire le même travail.

Build de référence connu bon : **`legacy-0917-viseme-1`**.

---

## 1. Les bras ne tournent pas avec le personnage

**Statut** — ouvert, diagnostic partiel.

### Symptôme

Quand un personnage se tourne, ses **bras restent attachés au corps mais pointent
dans la mauvaise direction** : le corps a tourné, les bras nont pas. Le résultat
ressemble à un bras cassé ou à une articulation qui se replie.

### Reproduction

Présent dans **les deux** modes d'animation :

| Mode | Comportement |
|---|---|
| `mood` | Bras attachés, mais dans la mauvaise direction |
| `fbx` | Identique |

Le fait que le mode `mood` soit concerné **élimine le clip FBX** comme cause.

### Déjà écarté

| Hypothèse | Résultat |
|---|---|
| Le clip FBX est mal orienté | Écarté — le symptôme existe sans clip |
| Deux writers se disputent les os | Écarté — en `mood` il n'y a qu'un seul writer |
| Les os sont détachés / orphelins | Écarté — les bras restent attachés |
| IK du pointeur (`touchAt`) | Écarté — `talkinghead.mjs:4066`, mais **du code mort ici** : ni le joueur ni le moteur n'enregistrent d'écouteur `pointermove`. Le moteur n'enregistre que `finished` (audio). |
| `speakTo` mal renseigné | Écarté — non défini dans ce build |

### Hypothèse principale

Deux autorités indépendantes sur le lacet (l'orientation) :

- Le joueur écrit `armature.rotation.y` — `faceEachOther` (`dialogue-player.html`
  L1062, L1116-1120) et `headCameraFor` (L1209).
- Le moteur garde **son propre** lacet : `bodyRotateY`, calculé `roty + droty`
  (`talkinghead.mjs:3850`, `3965`, `4045`).

Quand le joueur fait tourner un personnage, `armature.rotation.y` change mais l'état
interne du moteur ne suit pas. La pose est donc résolue pour **l'ancienne**
orientation. En `mood`, la pose des bras vient entièrement du moteur :
`poseBase` / `poseDelta` appliquent `LeftShoulder.rotation`, `LeftArm.rotation`
(`talkinghead.mjs:274-295`) et un `LeftHand.quaternion` aléatoire (L4163-4164).

### Question décisive — à trancher avant toute correction

> **Les bras sont-ils déjà dans la mauvaise direction au chargement, ou seulement
> après une rotation du personnage ?**

- **Faux dès le chargement** → la pose est écrite pour une orientation différente
  de celle donnée par les positions sauvegardées. indices : le layout sauvegardé de
  `ce2f201a` contient `"yaw": "92.1"` pour `SPEAKER_00`. Dans ce cas il s'agit
  peut-être d'un problème de layout sauvegardé et non d'animation.
- **Correct puis faux après une rotation** → c'est bien le conflit de lacets décrit
  ci-dessus.

### Piste de correction (non implémentée)

Renvoyer les rotations par le chemin du moteur (son `bodyRotateY`) au lieu
d'écrire `armature.rotation.y`, afin que la pose interne et l'orientation visible
bougent ensemble. Cote joueur uniquement : **ne pas modifier
`assets/talkinghead.mjs`**, qui est une copie tierce.

### Vérification

Instrumenter `armature.rotation.y`, le lacet mondial de l'os `Hips` et le lacet
interne du moteur au moment où les bras semblent faux. Si `Hips` et `armature`
divergent, le conflit est mesuré et non supposé.

> Rappel : le rendu headless (SwiftShader) ne rasterise pas correctement ces
> meshes skinnés. **La validation doit se faire dans un vrai navigateur.**

---

## 2. Personnages qui disparaissent à l'écran

**Statut** — ouvert, non reproduit.

Un ou plusieurs personnages sont visibles (audio playing, `armature.visible ===
true`,SDS correctly parented) mais n'apparaissent pas à l'écran pendant la lecture.

Éléments déjà vérifiés et écartés : drapeaux de visibilité, meshes `visible`,
NaN dans les matrices, seconde caméra, `frustumCulled` (déjà `false` partout),
dérive de l'horloge de lecture, reconstruction du dictionnaire `state.avatars`.

Piste restante la plus sérieuse : **audit au niveau des pixels**. Un contrôle
frontalier (NDC) disait « dans le cadre » alors que l'image était vide — les deux
ne concordaient pas, donc tous les contrôles géométriques sont suspects. Il faut
compter les pixels réellement rasterisés dans le rectangle projeté de chaque
personnage, **depuis un vrai navigateur**.
