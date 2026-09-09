# ProMLens — Reference technique

[English](DOC.md) | **Francais**

Version : **0.23.1**

---

## 1. Vue d'ensemble

ProMLens est une application web FastAPI + Vis.js. Elle combine une déclaration statique de topologie (`topology.yaml`) avec les métriques Prometheus en direct pour afficher un graphe réseau interactif dans le navigateur.

### Architecture

| Sujet | Où | Pourquoi |
|---|---|---|
| Proxy des requêtes Prometheus | Backend (tous les modes sauf `cert`) | Cache les identifiants au navigateur, évite les problèmes CORS |
| Requêtes Prometheus directes | Navigateur (mode `cert` uniquement) | Le certificat client est stocké dans le navigateur ; la négociation mTLS exige la participation du navigateur |
| Analyse de topology.yaml | Backend | Fichier côté serveur, non accessible directement depuis le navigateur |
| Persistance du layout | Backend (`layout.json`) + localStorage | Survit aux rechargements de page, partagé entre les sessions du navigateur |
| Construction du graphe (buildGraph) | Frontend (JavaScript) | Calcul pur sur des données déjà récupérées ; aucun aller-retour supplémentaire |
| Rendu Vis.js | Frontend | DOM/Canvas, doit s'exécuter dans le navigateur |
| Couleurs de seuils | Frontend | Calculées à partir des métriques déjà récupérées pour le graphe |
| Évaluation des alertes ProMLens (`local_alerts.py`) | Backend (tâche de fond) | Mêmes seuils que le graphe, mais doit continuer à évaluer quand aucun navigateur n'est ouvert |
| Notifications webhook d'alerte (`notifier.py`) | Backend (tâche de fond) | Doit continuer à émettre quand aucun navigateur n'est ouvert |

---

## 2. Installation

### Paquet Debian

```bash
# Build from source
make deb

# Install
dpkg -i promlens_*.deb

# Systemd service is enabled automatically
systemctl status promlens
```

Arborescence après installation :

| Chemin | Contenu |
|---|---|
| `/usr/lib/promlens/` | Fichiers source de l'application |
| `/etc/promlens/promlens.yaml` | Configuration de connexion à Prometheus |
| `/etc/promlens/topology.yaml` | Déclaration de la topologie réseau |
| `/var/lib/promlens/layout.json` | Positions de noeuds sauvegardées (écrit à l'exécution) |
| `/etc/default/promlens` | Surcharges par variables d'environnement |

### Conteneur

```bash
make build   # build the image
make run     # build + run, mounts examples/data/ at /data
```

Le conteneur lit par défaut sa configuration dans `/data/promlens.yaml` et `/data/topology.yaml`.
Surchargez avec des variables d'environnement (voir section 8).

---

## 3. Configuration — promlens.yaml

Ce fichier est lu à chaque requête. Toute modification prend effet immédiatement sans redémarrage du serveur (utilisez le bouton RELOAD de l'interface ou `POST /api/reload` pour forcer un rafraîchissement du graphe, ou activez [auto_reload](#option-auto_reload) pour reconstruire le graphe à l'enregistrement).

### Connexion Prometheus

```yaml
url: https://prometheus.example.com:9090

auth:
  type: none          # none | basic | bearer | cert
  username: admin     # basic uniquement
  password: secret    # basic uniquement
  token: mytoken      # bearer uniquement

instance_label: instance   # label Prometheus servant d'ID de noeud (par defaut: instance)
parent_label: parent       # label portant le nom du noeud parent (par defaut: parent)
guest_label: role          # label marquant un noeud comme invite (par defaut: job)
guest_values: [vm, lxc]    # valeurs de guest_label traitees comme invite (par defaut: [vm])
ssl_verify: true           # mettre false pour ignorer la verification TLS (deconseille)
timeout: 30                # timeout HTTP en secondes
proxy: http://proxy:8080   # proxy HTTP optionnel
```

### Options de premier niveau

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `url` | string | requis | URL de base de Prometheus |
| `instance_label` | string | `instance` | Label utilisé pour identifier chaque noeud |
| `parent_label` | string | `parent` | Label portant le nom du noeud parent |
| `guest_label` | string | `job` | Label dont la valeur marque un noeud comme invité |
| `guest_values` | list | `[vm]` | Valeurs de `guest_label` qui rattachent le noeud à `parent_label` |
| `ssl_verify` | bool | `true` | Vérification du certificat TLS |
| `timeout` | int | `30` | Timeout des requêtes HTTP en secondes |
| `proxy` | string | aucun | URL du proxy HTTP |
| `refresh` | int | `30` | Intervalle de rafraîchissement automatique affiché par défaut dans l'interface (secondes) |
| `auto_reload` | bool | `false` | Reconstruit le graphe dès que `promlens.yaml` ou `topology.yaml` change sur le disque |
| `direct_credentials` | bool | `false` | Mode cert uniquement : le navigateur envoie le certificat client dans les requêtes Prometheus directes |

### Option auto_reload

Declenche une reconstruction du graphe des que `topology.yaml` ou `promlens.yaml` change sur le disque. Le defaut est `false`.

```yaml
auto_reload: true
```

Les deux fichiers etaient deja relus a chaque cycle de rafraichissement : `GET /api/topology` et `GET /api/config` analysent leur fichier a chaque requete, donc une modification finissait toujours par etre prise en compte. Cette option change seulement **quand** la reconstruction est declenchee, pas ce qui est reconstruit. Sans elle, une modification apparait au bout de `refresh` secondes au maximum (30 par defaut) ou au clic sur RELOAD.

Avec `auto_reload: true`, l'interface interroge `GET /api/mtime` toutes les 5 secondes. Quand la date de modification de l'un des deux fichiers change, elle :

1. Appelle `POST /api/reload` pour valider les deux fichiers YAML.
2. Lance un rafraichissement complet (`fetchAll()`), uniquement si la validation a reussi.

Si la validation echoue -- typiquement un editeur qui ecrit un fichier a moitie enregistre -- le tour est saute et un warning est journalise dans la console du navigateur. Le prochain enregistrement est reessaye, donc une erreur d'analyse transitoire ne bloque jamais le graphe.

Avec `auto_reload: false` (le defaut), rien n'est interroge : le graphe se rafraichit toujours a son intervalle normal et le bouton RELOAD fonctionne toujours.

L'intervalle de poll de 5 secondes est une constante fixe (`AUTO_RELOAD_POLL_MS` dans `src/static/index.html`), il n'est pas configurable.

### Section app_auth

Contrôle l'accès à l'interface ProMLens elle-même, indépendamment de l'authentification Prometheus.

```yaml
app_auth:
  mode: basic                              # none | basic | cert
  htpasswd: /etc/promlens/.htpasswd        # requis pour le mode basic
  secret: "random-string-change-me"        # signe les cookies de session
  session_days: 7                          # duree de vie de la session
```

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `mode` | string | `none` | Mode d'authentification |
| `htpasswd` | path | aucun | Chemin du fichier htpasswd Apache (mode basic) |
| `secret` | string | aucun | Clé de signature HMAC des cookies de session. Doit être définie dans tout mode autre que none |
| `session_days` | int | `7` | Durée de vie du cookie de session en jours |

**Créer un fichier htpasswd :**

```bash
# Create file with first user (bcrypt recommended)
htpasswd -cB /etc/promlens/.htpasswd alice

# Add a user
htpasswd -B /etc/promlens/.htpasswd bob

# Remove a user
htpasswd -D /etc/promlens/.htpasswd alice
```

Formats supportés : bcrypt (`$2y$`), Apache MD5 (`$apr1$`), SHA1 (`{SHA}`).

### Section libvirt

Ajoute l'état des VM dans les infobulles des noeuds hyperviseurs **et crée des noeuds VM sur la carte** pour les VM qui n'ont pas leur propre instance node_exporter.

Les VM déjà présentes sur la carte (via topology.yaml ou node_exporter) ne sont pas dupliquées. Les noeuds VM créés automatiquement sont reliés à leur hyperviseur par un lien en pointillés et colorés selon l'état libvirt (vert=running, orange=paused/suspended, rouge=shut off/crashed).

```yaml
libvirt:
  enabled: true                  # mettre false pour desactiver sans supprimer la section
  instance_label: instance       # label identifiant l'hyperviseur (par defaut: instance_label global)
  metric: libvirt_domain_info_state  # metrique PromQL a interroger (par defaut: libvirt_domain_info_state)
```

Supprimez la section entièrement ou mettez `enabled: false` pour désactiver.

### Section blackbox

Active la visualisation des sondes du blackbox exporter. ProMLens interroge `probe_success` pour quatre rôles fixes, chacun rendu différemment. Les noms des modules derrière chaque rôle sont configurables via `modules` ; les rôles eux-mêmes ne le sont pas.

```yaml
blackbox:
  enabled: true
  destination_label: instance    # label identifiant la cible de la sonde (par defaut: instance)
  source_label: job              # label identifiant la source de la sonde; si absent, affiche "prometheus"
  prometheus_node: mon01         # noeud de topologie qui heberge Prometheus (source par defaut des sondes)
  http_node_label: upstream      # label des sondes HTTP/HTTPS identifiant le noeud (par defaut: upstream)
  modules:                       # noms des modules blackbox interroges, par role
    icmp: [icmp]                 # defaut
    ssh:  [ssh_banner]           # defaut
    tcp:  [tcp_connect]          # defaut
    http: [http_2xx, https_2xx]  # defaut
  dest_aliases:
    mynode:                      # ID de noeud de topologie
      - alias-one                # valeurs de destination_label associees a mynode
      - alias-two
  source_aliases:
    mynode:                      # ID de noeud de topologie
      - alias-one                # valeurs de source_label associees a mynode
      - alias-two
```

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active/désactive sans supprimer la section |
| `destination_label` | string | `instance` | Label qui contient l'identifiant de la cible de sonde ; peut pointer sur un label de groupe partage par plusieurs sondes (voir ci-dessous). Une serie sans ce label retombe sur `instance_label`, puis sur `instance` |
| `source_label` | string | aucun | Label qui contient l'identifiant de la source de sonde |
| `prometheus_node` | string | aucun | Noeud qui héberge Prometheus. Les sondes dont la source est inconnue partent de ce noeud au lieu du noeud `prometheus` autonome. Accepte un ID de noeud de topologie, une IP, un label ou une instance/hostname Prometheus |
| `http_node_label` | string | `upstream` | Label identifiant le noeud pour les sondes HTTP/HTTPS. Une serie sans ce label retombe sur `upstream`, puis sur `parent_label` ; une serie qui n'a aucun des trois est ignoree |
| `modules` | map | voir ci-dessous | Noms des modules blackbox interrogés pour chaque rôle |
| `dest_aliases` | map | `{}` | Associe des ID de noeuds de topologie à des listes d'alias de cibles de sonde. Mapping explicite : il est prioritaire sur toute autre résolution de nom (exacte ou fuzzy) pour la destination d'une sonde. Voir ci-dessous |
| `source_aliases` | map | `{}` | Associe des ID de noeuds de topologie à des listes d'alias de sources de sonde. Mapping explicite : il est prioritaire sur toute autre résolution de nom (exacte ou fuzzy) pour la source d'une sonde. Voir ci-dessous |

#### blackbox.modules

| Rôle | Modules par défaut | Rendu |
|---|---|---|
| `icmp` | `icmp` | Arêtes de sonde dans le graphe, filtrées par le bouton ICMP |
| `ssh` | `ssh_banner` | Arêtes de sonde dans le graphe, filtrées par le bouton SSH |
| `tcp` | `tcp_connect` | État des services TCP dans les tooltips de noeud |
| `http` | `http_2xx`, `https_2xx` | État des cibles HTTP/HTTPS dans les tooltips de noeud |

Règles :

- Chaque rôle accepte une chaîne seule (`ssh: ssh_banner`) ou une liste de noms de modules.
- Un rôle omis garde son défaut. Une clé `modules` absente garde tous les défauts.
- Une liste vide désactive complètement le rôle : sa requête est ignorée.
- Plusieurs modules sur un rôle sont fusionnés en une seule requête : `probe_success{module=~"http_2xx|https_2xx"}`.
- Les noms de modules sont validés contre `^[A-Za-z0-9_.:?*+|()\[\]-]+$`. Une entrée invalide est ignorée avec un warning dans les logs. Les métacaractères de regex sont autorisés, donc `http: ["https?_2xx"]` est un équivalent valide en une entrée.

Exemple : interroger le HTTP via un nom de module personnalisé et désactiver la section TCP des tooltips.

```yaml
blackbox:
  enabled: true
  modules:
    http: [http_2xx_internal, https_2xx_internal]
    tcp:  []                     # aucune requete TCP
```

La map résolue (config fusionnée avec les défauts) est renvoyée par `GET /api/config` dans `blackbox.modules`.

#### blackbox.dest_aliases

Rattache les sondes dont le nom de cible ne correspond à aucun noeud du graphe. Deux formes sont gérées, toutes deux avec la config `dest_aliases: {mynode: [alias-one, alias-two]}` :

| Cible de sonde | Forme | Résultat |
|---|---|---|
| `alias-one` | Alias exact | La sonde est rattachée à `mynode` |
| `alias-one-server` | Alias en préfixe | `alias-one` est retiré, le reste `server` est résolu comme un nom de noeud (exact, puis fuzzy) |

Le mapping est explicite : il est prioritaire sur toute autre résolution de nom (exacte ou fuzzy) pour le choix de la destination d'une sonde. Une cible aliasée ne crée plus de noeud sonde orphelin : l'arête de sonde arrive sur le noeud cible.

Les tooltips (noeud et arête) et le panneau des issues nomment la sonde par la valeur de son `destination_label`, c'est-à-dire l'alias tel qu'il est écrit dans la config (`alias-one`), et non par le noeud cible résolu (`mynode`). L'arête, elle, continue de pointer vers le noeud réel : `alias-one` et `alias-two` partagent une seule arête vers `mynode` tout en gardant une ligne chacun dans les tooltips. Ce nommage par alias est prioritaire même quand `destination_label` est détecté comme un label de groupe, et le clic sur l'arête ouvre la requête Prometheus avec le bon label : `destination_label` pour une cible aliasée.

Une clé de mapping qui ne désigne aucun noeud du graphe est ignorée, et le panneau des warnings affiche :

```
blackbox.dest_aliases target not resolved: "mynode"
```

#### blackbox.source_aliases

Le pendant de `dest_aliases` pour le `source_label`. Même format : la clé est un ID de noeud de topologie, la valeur est la liste des valeurs de `source_label` qui désignent ce noeud.

Une valeur de `source_label` ne correspondant à aucun noeud faisait repartir la sonde du noeud Prometheus par défaut (`prometheus_node`, ou le noeud `prometheus` autonome). Avec `source_aliases: {mynode: [alias-one, alias-two]}`, une sonde dont la source est `alias-one` part de `mynode`.

Le mapping est explicite : il est prioritaire sur toute autre résolution de nom (exacte ou fuzzy) pour le choix de la source d'une sonde.

Les tooltips nomment la source par la valeur de son `source_label`, c'est-à-dire l'alias tel qu'il est écrit dans la config (`alias-one`), et non par le noeud source résolu (`mynode`). Plusieurs alias sources partageant un noeud restent donc distinguables : le tooltip d'arête affiche une ligne `sources:` quand il y en a plusieurs, et chaque ligne de sonde est suffixée par `(alias -> cible)`.

Une clé de mapping qui ne désigne aucun noeud du graphe est ignorée, et le panneau des warnings affiche :

```
blackbox.source_aliases target not resolved: "mynode"
```

Contrairement aux destinations, une source non résolue n'a jamais créé de noeud orphelin : elle retombait simplement sur le noeud Prometheus.

#### Grouper plusieurs sondes sur un noeud

`destination_label` pointe normalement sur un label unique par sonde (le defaut `instance`). Il peut aussi pointer sur un label de groupe, pour rattacher plusieurs sondes au meme noeud de topologie :

```yaml
blackbox:
  enabled: true
  destination_label: parent      # plusieurs sondes partagent parent="synacktiv"
```

Chaque sonde conserve sa cible reelle, lue comme la valeur de `destination_label`, puis la valeur de `instance_label`, puis `instance`. L'ordre s'inverse quand `destination_label` pointe sur un label de groupe : une valeur de groupe ne peut pas nommer chaque sonde, donc la cible est lue comme la valeur de `instance_label`, puis `instance`, puis la valeur de `destination_label`. Les sondes sont dedupliquees sur cette cible, donc aucune sonde n'est perdue et aucune sonde en echec n'est masquee.

**La detection de groupe est automatique.** Aucune option ne declare un label de groupe. `destination_label` est traite comme un label de groupe des qu'une de ses valeurs couvre plusieurs cibles de sonde distinctes (valeurs `instance_label`/`instance` distinctes) venant de la meme source (valeur de `source_label`, ou aucune source). Le test est fait une fois par config sur l'ensemble des series ICMP et SSH, donc un groupe ne contenant qu'une sonde se comporte comme les autres. Des sondes qui atteignent la meme destination depuis des sources differentes ne forment pas un groupe.

Lire `destination_label` en premier compte dans les configurations multi-sources ou `instance` contient l'exporteur blackbox ou l'hote source : l'ancien ordre affichait des lignes du type `ICMP server1 -> server1`.

Dans le tooltip du noeud, chaque ligne de sonde nomme sa propre cible, quel que soit le nombre de sondes sur le noeud. Une sonde unique nomme l'hote sonde, pas le noeud :

```
Probe:
  v ICMP prometheus -> dojo2-orange-alarme
```

Avec plusieurs sondes, une ligne par cible :

```
Probe:
  v ICMP prometheus -> dojo2-orange-alarme
  v ICMP prometheus -> dojo2-orange-cam
  x ICMP prometheus -> 10.0.7.9
```

Sur une configuration par defaut (`destination_label: instance`), la cible et le nom du noeud sont la meme chaine, donc le tooltip se lit exactement comme avant. Le libelle ne change que quand `destination_label` pointe sur un label de groupe. Une serie dont la cible ne peut pas etre resolue retombe sur le nom du noeud.

Consequences :

- **Etat du noeud** : le noeud passe en orange des qu'une seule de ses sondes est down.
- **Panneau issues** : il suit sa propre regle, car son entree commence deja par le label du noeud. Les sondes en echec sont listees `icmp <cible>` quand le noeud porte plusieurs cibles, `icmp` seul sinon. Les entrees sont dedupliquees.
- **Lien du graphe** : le lien entre les deux noeuds garde une entree par sonde. Son tooltip affiche une ligne `targets:` et tague chaque ligne de sonde avec sa cible.
- **Clic sur le lien** : ouvre `probe_success{<destination_label>="<cible>"}` sur la cible reelle de la sonde. La requete utilise le label depuis lequel la cible a ete lue, donc elle devient `probe_success{instance="<cible>"}` (le label `instance_label`) quand `destination_label` est un label de groupe.

**Series sans le label de groupe.** Une sonde qui ne porte pas le label configure n'est plus perdue : elle retombe sur le label par defaut de l'option qui lui manque.

| Role | Cle de noeud | Repli quand le label configure est absent |
|---|---|---|
| `icmp`, `ssh` | `destination_label` | valeur de `instance_label`, puis `instance` |
| `tcp` | `instance_label` | `instance` |
| `http` | `http_node_label` | `upstream`, puis `parent_label`, puis la serie est ignoree |

Avec `destination_label: parent` et `http_node_label: parent`, une serie ICMP portant `instance="orphan-host"` mais aucun label `parent` se rattache au noeud resolu depuis `orphan-host` au lieu de disparaitre, donc son etat DOWN reste visible. Une serie HTTP portant `upstream="synacktiv"` mais aucun label `parent` se rattache a `synacktiv`.

Il n'y a volontairement aucun repli sur `instance` pour le role HTTP : le label `instance` d'une sonde HTTP contient une URL, et le decoupage du port (dernier deux-points) transformerait `https://c.example` en un faux hote nomme `https`. Une serie HTTP qui ne porte ni le label configure, ni `upstream`, ni `parent_label` reste ignoree.

Les configurations utilisant le defaut `destination_label: instance` ne sont pas affectees : la valeur de destination et la cible de la sonde sont la meme chaine, et le label configure est aussi le label de repli.

### Section frigate

Ajoute un noeud par caméra du NVR Frigate à partir de la métrique `frigate_camera_fps`. `fps > 0` = en ligne (vert) ; `fps = 0` = hors ligne (rouge). Cliquer sur un noeud caméra ouvre `camera_url/#camera_name`.

```yaml
frigate:
  enabled: true
  camera_url: https://frigate.example.com
```

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Active/désactive sans supprimer la section |
| `camera_url` | string | aucun | URL de base de l'interface web Frigate |

Ordre de résolution du parent d'une caméra : surcharge dans la section `cameras` de topology.yaml > label `parent` dans `frigate_camera_fps` > repli sur le premier router ou switch.

### Section thresholds

Définit les ratios de métriques auxquels les couleurs des noeuds et des liens passent du vert à l'orange, puis de l'orange au rouge. Les deux sections sont optionnelles. Les valeurs par défaut sont `orange: 0.70` et `red: 0.90` pour toutes les métriques.

Le niveau `red` est aussi ce qui déclenche une notification webhook (voir [webhooks](#section-webhooks)) : augmenter ce seuil pour un noeud supprime à la fois sa couleur rouge et ses notifications.

Métriques supportées : `cpu`, `mem`, `disk`, `network`.

Les deux valeurs doivent être dans `[0.0, 1.0]` et `orange` doit être strictement inférieur à `red`.

```yaml
thresholds:
  cpu:     { orange: 0.70, red: 0.90 }
  mem:     { orange: 0.70, red: 0.90 }
  disk:    { orange: 0.80, red: 0.95 }
  network: { orange: 0.60, red: 0.85 }
```

Seules les métriques que vous listez sont surchargées. Les métriques omises conservent les valeurs par défaut.

### Section thresholds_by_node

Surcharge les seuils pour des noeuds individuels. La clé est l'ID de topologie (le champ `id` dans `topology.yaml`). Seules les métriques listées sont surchargées pour ce noeud ; toutes les autres retombent sur les valeurs génériques de `thresholds` (ou sur les valeurs par défaut intégrées).

```yaml
thresholds_by_node:
  hv01:
    cpu:  { orange: 0.85, red: 0.95 }
  db01:
    disk: { orange: 0.80, red: 0.95 }
```

Dans l'exemple ci-dessus, `hv01` utilise un seuil CPU plus élevé (charge soutenue attendue), tandis que `db01` utilise un seuil disque plus strict. Toutes les autres métriques de ces deux noeuds utilisent les valeurs de `thresholds` ou les valeurs par défaut.

### Option threshold_colors

Contrôle si les couleurs de seuil CPU/mémoire/disque/réseau sont appliquées aux noeuds et aux liens. La valeur par défaut est `true`.

```yaml
threshold_colors: false
```

Quand la valeur est `false` :

- Les noeuds qui sont UP et sans échec de sonde utilisent leur couleur neutre propre à leur type (les noeuds de topologie conservent la couleur de leur icône ; les noeuds purement Prometheus utilisent `#c9d1e8`).
- Les couleurs de seuil de bande passante réseau des liens (orange/rouge) sont supprimées. Les liens n'affichent plus que du vert (lien up) ou du rouge (lien down).
- La coloration par seuil des tunnels WireGuard est également désactivée.

Les couleurs suivantes restent actives quel que soit ce réglage :

| Condition | Couleur |
|---|---|
| Noeud down | rouge `#f04f4f` |
| Échec de sonde (blackbox ICMP/TCP/HTTP) | orange `#e0972a` |
| Surcouche d'alerte Prometheus | couleur de la sévérité de l'alerte |

Mettez cette option à `false` quand le bruit des seuils n'est pas utile (par exemple un tableau d'affichage) et que vous voulez une vue de base plus lisible.

### Section webhooks

Envoie une notification webhook lorsqu'une **alerte ProMLens** démarre ou se termine. Ces alertes sont calculées par ProMLens lui-même à partir des métriques brutes de node_exporter : aucune règle d'alerte Prometheus ni Alertmanager n'est nécessaire. Les règles sont listées dans [Alertes ProMLens](#alertes-promlens) ci-dessous.

Les notifications sont produites côté serveur par une tâche de fond : elles ne nécessitent donc pas qu'un navigateur soit ouvert.

Les alertes Prometheus affichées dans l'interface (panneau d'alertes, surcouches sur les noeuds, page des alertes) proviennent de `/api/v1/alerts` et constituent un flux **distinct** : elles sont affichées mais jamais notifiées. Inversement, les alertes ProMLens sont notifiées mais pas affichées dans le graphe — elles ne pilotent que les couleurs de noeuds dont elles dérivent.

```yaml
webhooks:
  enabled: true                          # supprimer la section ou mettre false pour desactiver
  interval: 60                           # secondes entre deux sondages (par defaut: 60, minimum: 10)
  for_cycles: 2                          # cycles consecutifs avant notification (par defaut: 2, minimum: 1)
  notify_resolved: true                  # notifie aussi la fin d'une alerte (par defaut: true)
  link: https://promlens.example.com/    # valeur du champ "url" du payload
  severities: [critical]                 # ne notifie que ces severites (par defaut: toutes)
  disabled:                              # motifs glob d'alertname jamais notifies
    - SystemdUnitFailed
    - "*High"
  timeout: 10                            # timeout HTTP du POST webhook (par defaut: 10)
  ssl_verify: true                       # verification TLS du endpoint webhook (par defaut: true)
  ca_file: /etc/promlens/webhook-ca.crt  # bundle CA servant a verifier le endpoint webhook
  proxy: http://proxy:8080               # proxy HTTP optionnel pour le POST webhook
  targets:
    - url: https://notify.example.com/api/notify
      topic: infra
      recipients: [ops, oncall]
      module: promlens
      link: https://promlens.example.com/     # remplace le link global pour cette cible (optionnel)
      headers:
        Authorization: "Bearer ${NOTIFY_TOKEN}"
```

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `true` quand la section existe | Interrupteur principal ; forcé à false quand aucune cible valide n'est définie |
| `interval` | int | `60` | Intervalle de sondage en secondes, borné à 10 s minimum |
| `for_cycles` | int | `2` | Nombre de sondages consécutifs pendant lesquels une condition doit tenir avant d'être notifiée, borné à 1 minimum |
| `notify_resolved` | bool | `true` | Envoie une notification `resolved` quand une alerte active se termine |
| `link` | string | aucun | Valeur par défaut du champ `url` du payload |
| `severities` | list | toutes | Seules les alertes dont le label `severity` est dans cette liste sont notifiées |
| `disabled` | list | vide | Motifs glob comparés à `alertname` (syntaxe fnmatch) |
| `timeout` | int | `10` | Timeout HTTP total de chaque POST webhook |
| `ssl_verify` | bool | `true` | Vérification TLS pour le endpoint webhook |
| `ca_file` | string | aucun | Chemin d'un bundle CA PEM utilisé pour vérifier le endpoint webhook à la place du magasin de CA système |
| `proxy` | string | aucun | Proxy HTTP utilisé pour le POST webhook |
| `targets[].url` | string | requis | Endpoint webhook ; une cible sans `url` est ignorée |
| `targets[].topic` | string | `""` | Champ `topic` du payload |
| `targets[].recipients` | list ou string | vide | Champ `recipients` du payload (un scalaire est encapsulé dans une liste) |
| `targets[].module` | string | `promlens` | Champ `module` du payload |
| `targets[].link` | string | `link` global | Champ `url` du payload pour cette cible |
| `targets[].headers` | map | vide | En-têtes HTTP supplémentaires envoyés avec le POST |

`ca_file` s'applique à toutes les cibles : il remplace le magasin de CA système lors de la vérification du certificat TLS des endpoints webhook, ce dont ont besoin les PKI internes. Il est ignoré quand `ssl_verify: false`, et un fichier illisible ou mal formé provoque un repli sur le magasin de CA système avec un avertissement, plutôt qu'une désactivation silencieuse de la vérification. Dans un conteneur, le fichier doit être monté et le chemin doit être celui vu depuis l'intérieur du conteneur (par exemple sous `/data`).

`$VAR` et `${VAR}` sont substitués depuis l'environnement du processus dans `ca_file` ainsi que dans `targets[].url`, `topic`, `recipients`, `module`, `link` et dans chaque valeur d'en-tête. Conservez les jetons dans `/etc/default/promlens` plutôt que dans `promlens.yaml`.

#### Alertes ProMLens

À chaque cycle, le notifier exécute huit requêtes instantanées et évalue les règles suivantes sur leur résultat. Toutes utilisent le label `instance_label` pour identifier un noeud.

| `alertname` | Condition | Label supplémentaire | Résumé |
|---|---|---|---|
| `NodeDown` | `up{exporter="node"} != 1` | aucun | `node is down (up == 0)` |
| `CpuHigh` | `1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))` >= seuil `red` de `cpu` | aucun | `cpu 93% (threshold 90%)` |
| `MemoryHigh` | `1 - MemAvailable / MemTotal` >= seuil `red` de `mem` | aucun | `memory 93% (threshold 90%)` |
| `DiskHigh` | `1 - avail / size` sur `mountpoint="/"` >= seuil `red` de `disk` | aucun | `disk 93% (threshold 90%)` |
| `NetworkHigh` | interface la plus chargée, `max(rx, tx) / node_network_speed_bytes` >= seuil `red` de `network` | aucun | `network 93% (threshold 90%)` |
| `SystemdUnitFailed` | `node_systemd_unit_state{state="failed"} == 1` | `unit` | `systemd unit borg.service is failed` |

Notes :

- Chaque alerte porte `severity: critical`. Seul le seuil **red** notifie ; le niveau orange reste un signal purement visuel, donc un noeud qui passe à l'orange dans le graphe ne réveille personne.
- Les seuils sont ceux des sections [thresholds](#section-thresholds) et [thresholds_by_node](#section-thresholds_by_node), résolus exactement comme l'interface les résout (le label d'instance est comparé aux clés de surcharge avec et sans son port). Ce qui fait passer un noeud au rouge dans le graphe est ce qui notifie.
- `threshold_colors: false` ne désactive que les couleurs dans l'interface ; cela ne désactive pas les notifications.
- Un noeud déclaré down ne produit aucune autre alerte pour ce cycle : les métriques que Prometheus renvoie encore pour lui sont périmées.
- La capacité d'interface retombe sur 1 Gbit/s quand `node_network_speed_bytes` est absent, comme dans l'interface.
- Les noeuds sont énumérés depuis `up{exporter="node"}` : un noeud absent de cette requête n'est donc pas évalué du tout.

#### Payload

Chaque événement produit un POST par cible avec un corps JSON comportant exactement six champs :

```json
{
  "title": "[FIRING] DiskHigh - node2",
  "message": "disk 95% (threshold 90%)\nseverity: critical\ninstance: node2\nsince: 2026-08-26T11:00:00Z",
  "url": "https://promlens.example.com/",
  "topic": "infra",
  "recipients": ["ops", "oncall"],
  "module": "promlens"
}
```

| Champ | Contenu |
|---|---|
| `title` | `[FIRING]` ou `[RESOLVED]`, l'`alertname` et le noeud (label `instance_label`, avec repli sur `instance`) |
| `message` | Annotation `summary` de l'alerte, puis `severity`, le noeud et `activeAt` (le premier sondage où la condition a été vue) |
| `url` | `targets[].link`, sinon le `link` global |
| `topic` | `targets[].topic` |
| `recipients` | `targets[].recipients` |
| `module` | `targets[].module` |

#### Cycle de vie des notifications

1. Toutes les `interval` secondes, `promlens.yaml` est relu et les métriques sont sondées. Les changements de configuration s'appliquent sans redémarrer le service. Le sondage utilise le `timeout` de premier niveau (pas celui de `webhooks`, qui ne s'applique qu'au POST) ; à son expiration, le cycle est journalisé comme `webhook notifier cycle failed (metrics poll): TimeoutError: no answer from <url> within <n>s` puis retenté à l'intervalle suivant.
2. Les alertes sont identifiées par l'ensemble trié complet de leurs labels : la même condition sur deux noeuds — ou deux units en échec sur un même noeud — est donc suivie séparément.
3. Une condition doit tenir sur `for_cycles` sondages consécutifs avant d'être notifiée, ce qui filtre les pics qu'un sondage unique transformerait sinon en paire firing/resolved. Avec les valeurs par défaut, un pic CPU doit durer deux minutes pour notifier. Une condition qui disparaît avant d'être confirmée est simplement oubliée.
4. Le premier cycle après le démarrage (ou après la réactivation de la section) ne fait qu'enregistrer l'état courant ; les conditions déjà actives ne génèrent pas de notification.
5. Une condition confirmée absente du cycle précédent produit un événement `firing` ; une alerte précédemment active dont la condition a disparu produit un événement `resolved` quand `notify_resolved` vaut true. La résolution est immédiate — `for_cycles` ne retarde que le côté firing.
6. `disabled` et `severities` s'appliquent aux événements, pas à l'état suivi : ajouter un motif pendant qu'une alerte est active ne fabrique donc pas une fausse notification `resolved`.
7. Un webhook en échec (timeout, erreur de connexion, HTTP 4xx/5xx) est journalisé en avertissement et n'interrompt jamais la boucle. Il n'y a pas de réessai : l'événement est perdu.

#### Désactiver une seule alerte

Ajoutez son `alertname` à `disabled`. Les motifs glob sont supportés :

```yaml
webhooks:
  disabled:
    - SystemdUnitFailed    # nom exact
    - "Network*"           # toute alerte dont le nom commence par Network
    - "*High"              # toute alerte de seuil
```

Les couleurs des noeuds dans l'interface ne sont pas affectées ; seules les notifications sont supprimées.

---

## 4. Modes d'authentification

### Mode d'authentification Prometheus : `cert` (mTLS)

Quand `auth.type: cert`, le backend ne relaie pas les requêtes PromQL. Le navigateur les exécute directement contre Prometheus.

- Le certificat client doit être installé dans le navigateur (ou dans le magasin de l'OS).
- Prometheus doit avoir CORS activé : `--web.cors.origin="https://promlens.example.com"`
- `direct_credentials: false` (défaut) — le navigateur envoie les requêtes sans identifiants. À utiliser quand Prometheus est sur la même origine ou accessible sans mTLS depuis le navigateur.
- `direct_credentials: true` — le navigateur envoie le certificat client avec chaque requête. Nécessite que Prometheus renvoie `Access-Control-Allow-Credentials: true` et un `Access-Control-Allow-Origin` non générique.

Les champs `username`, `password`, `token`, `proxy` et `ssl_verify` sont ignorés en mode cert.

### Mode d'authentification applicative : `cert` (délégation nginx)

Quand `app_auth.mode: cert`, il n'y a pas de page de connexion. L'authentification est déléguée à un reverse proxy (nginx) qui :

1. Termine le TLS et vérifie le certificat client.
2. Injecte l'en-tête `x-remote-user: <username>`.

Si l'en-tête est absent, l'application renvoie HTTP 403 (HTML) ou HTTP 401 (JSON pour les endpoints d'API).

> **Changé en 0.15.0** — l'en-tête lu par ce mode a été renommé en `x-remote-user`. Lors d'une mise à jour depuis la 0.14.x, adaptez la ligne `proxy_set_header` de votre configuration nginx, sinon toutes les requêtes sont rejetées. Le fichier `/usr/share/promlens/nginx/routes.conf` livré est déjà à jour.

**Exemple nginx :**

```nginx
location / {
    proxy_pass http://127.0.0.1:8001;
    proxy_set_header x-remote-user $ssl_client_s_dn_cn;
    if ($ssl_client_verify != SUCCESS) { return 403; }
}
```

---

## 5. Configuration — topology.yaml

### nodes

Les noeuds de topologie sont toujours affichés, avec ou sans instance Prometheus correspondante. Si une instance Prometheus correspond à l'`id` du noeud (ou à son `nodename` issu de `node_uname_info`), ils sont fusionnés : le noeud de topologie reçoit l'icône serveur et les couleurs de métriques en temps réel.

```yaml
nodes:
  - id: gw
    label: Gateway
    type: router
    parent: cloud1
    interface: ppp0
    ip: 192.168.1.1
    inherit_zone: true
    children:
      - id: sw1
        type: switch
        interface: eth0
```

| Champ | Requis | Description |
|---|---|---|
| `id` | oui | Identifiant unique. Peut correspondre à un nom d'hôte Prometheus |
| `label` | non | Nom affiché (défaut : `id`) |
| `type` | oui | `cloud`, `router`, `firewall`, `switch`, `server`, `vm`, `lxc`, `kube`, `zigbee`, `wifi`. Absent ou inconnu : icône de repli et avertissement (voir ci-dessous) |
| `parent` | non | ID du noeud parent (crée un lien) |
| `children` | non | Liste de noeuds enfants — équivalent à définir `parent` sur chaque enfant |
| `interface` | non | Interface à afficher dans l'infobulle du lien vers le parent |
| `ip` | non | Adresse IP utilisée pour la résolution du noeud |
| `inherit_zone` | non | Mettre `false` pour exclure ce noeud de l'héritage automatique de zone (défaut : `true`) |

**Type absent ou inconnu :**

`type` reste obligatoire, mais un noeud qui en est dépourvu — ou qui porte un type inconnu de cette version — est quand même dessiné avec une icône de repli : `server` si l'`id` du noeud correspond à un hôte Prometheus (noeud fusionné), `switch` sinon. Ce repli est signalé dans le panneau des avertissements :

```
topology node "mynode": missing type
topology node "mynode": unknown type "nas"
```

Auparavant le repli était silencieux et affichait toujours l'icône switch, ce qui pouvait laisser croire qu'un serveur monitoré était un switch.

**Couleurs par type de noeud :**

| Type | Couleur |
|---|---|
| `cloud` | gris `#7e8aaa` |
| `router` | cyan `#00cfff` |
| `firewall` | orange `#e0972a` |
| `switch` | bleu `#4a8adf` |
| `server` | vert `#2dba6e` |
| `vm` | violet `#c084fc` |
| `lxc` | vert `#34d399` |
| `kube` | indigo `#818cf8` |
| `zigbee` | turquoise `#00bcd4` |
| `wifi` | bleu clair `#4fc3f7` |

### networks

Rattache automatiquement les hôtes Prometheus à un noeud passerelle par correspondance CIDR sur l'IP de l'instance.

```yaml
networks:
  - cidr: 192.168.1.0/24
    gateway: gw
    label: LAN
```

| Champ | Description |
|---|---|
| `cidr` | Plage IP en notation CIDR |
| `gateway` | ID du noeud auquel rattacher les hôtes correspondants |
| `label` | Nom d'affichage optionnel |

### zones

Bulles de regroupement visuel. Les hôtes portant un label Prometheus `zone=<id>` sont automatiquement placés dans la bulle de zone correspondante.

```yaml
zones:
  - id: prod
    label: Production
    parent: sw-server
    default_parent: sw-server
    members:
      - somehost
      - another-topology-node
```

| Champ | Requis | Description |
|---|---|---|
| `id` | oui | ID de zone unique |
| `label` | non | Libellé affiché dans la bulle (défaut : `id`) |
| `parent` | oui | Ancrage visuel — ID d'un noeud de topologie ou d'une autre zone |
| `default_parent` | non | Noeud parent des hôtes Prometheus de cette zone (si différent de l'ancrage visuel) |
| `members` | non | Noeuds supplémentaires ajoutés manuellement à la bulle |

Trois façons d'appartenir à une zone (cumulatives) :
- **Automatique** : l'instance Prometheus porte le label `zone=<id>`.
- **Manuelle** : le noeud est listé dans `members`.
- **Héritée** : les noeuds de topologie enfants d'un membre de la zone sont ajoutés automatiquement, sauf si `inherit_zone: false`.

Les zones imbriquées sont supportées. Seules les zones racines (dont le parent est un noeud de topologie) affichent une bulle.

### tunnels

Liens en pointillés entre noeuds, avec les métriques RX/TX de l'interface WireGuard déclarée.

```yaml
tunnels:
  - from: gw
    to:
      - peer1
      - peer2
    interface: wg0
    interface_to: wg0          # optionnel, vaut interface par defaut
    parent_interface: ppp0     # optionnel: interface physique servant de reference de capacite
```

| Champ | Description |
|---|---|
| `from` | ID du noeud source (topologie ou nom d'hôte Prometheus) |
| `to` | ID du noeud cible ou liste d'ID |
| `interface` | Interface WireGuard sur `from` |
| `interface_to` | Interface WireGuard sur `to` (défaut : identique à `interface`) |
| `parent_interface` | Interface physique dont le `node_network_speed_bytes` sert de capacité du lien |

Les interfaces de tunnel sont automatiquement exclues des infobulles de liens normaux pour les mêmes noeuds. Si une cible est inconnue de Prometheus, un noeud fantôme grisé est créé.

### links

Filtre les interfaces qui apparaissent dans les infobulles de liens. Sans déclaration `links`, toutes les interfaces non exclues sont affichées.

```yaml
links:
  - from: host1
    to:
      - host2
      - host3
    interface: eth0              # interface cote `from` (chaine ou liste)
    interface_to: eth1           # interface cote `to` (optionnel, vaut interface par defaut)
```

### cameras

Surcharge le noeud parent de caméras Frigate spécifiques. Sans cette section, le parent provient du label `parent` dans `frigate_camera_fps`.

```yaml
cameras:
  - name: front-door
    parent: sw-cameras
  - name: garage
    parent: sw-cameras
```

---

## 6. Référence de l'API

### POST /api/query

Relaie une requête PromQL vers Prometheus. Limité à 60 requêtes par minute et par IP.

**Corps de la requête :**

```json
{
  "metric": "up{exporter=\"node\"}",
  "mode": "instant",
  "time": null,
  "start": null,
  "end": null,
  "step": "60s"
}
```

| Champ | Type | Défaut | Description |
|---|---|---|---|
| `metric` | string | requis | Expression PromQL (2000 caractères max) |
| `mode` | string | `instant` | `instant` ou `range` |
| `time` | string | null | Horodatage d'évaluation (RFC3339 ou epoch Unix) |
| `start` / `end` | string | null | Requis pour les requêtes range |
| `step` | string | `60s` | Résolution de la plage |

**Réponse :**

```json
{
  "metric": "up{exporter=\"node\"}",
  "result_type": "vector",
  "count": 2,
  "instances": [
    {
      "labels": {"instance": "server1:9100", "job": "node"},
      "value": "1",
      "timestamp": 1748000000.0
    }
  ]
}
```

**Erreurs :**

| HTTP | Cause |
|---|---|
| 400 | Échec de la requête (erreur Prometheus ou problème réseau) |
| 422 | `start`/`end` manquants pour une requête range |
| 429 | Limite de débit dépassée (60 req/min par IP) |
| 503 | `promlens.yaml` introuvable ou champ `url` manquant |

```bash
curl -s -X POST http://127.0.0.1:8001/api/query \
  -H "Content-Type: application/json" \
  -d '{"metric":"up{exporter=\"node\"}"}' | jq .
```

### GET /api/config

Renvoie l'URL Prometheus (sans identifiants), le type d'authentification et les intégrations activées. Utilisé par le frontend pour configurer le routage des requêtes.

```bash
curl -s http://127.0.0.1:8001/api/config | jq .
```

```json
{
  "url": "http://prometheus.example.com:9090",
  "configured": true,
  "auth_type": "none",
  "instance_label": "instance",
  "parent_label": "parent",
  "guest_label": "job",
  "guest_values": ["vm"],
  "direct_credentials": false,
  "refresh": 30,
  "blackbox": {
    "destination_label": "instance",
    "modules": {
      "icmp": ["icmp"],
      "ssh":  ["ssh_banner"],
      "tcp":  ["tcp_connect"],
      "http": ["http_2xx", "https_2xx"]
    }
  },
  "libvirt": null,
  "frigate": {"camera_url": "https://frigate.example.com"},
  "app_auth_mode": "none",
  "app_auth_user": null
}
```

Quand une section (`blackbox`, `libvirt`, `frigate`) est absente de `promlens.yaml`, elle renvoie `null`. Quand elle est présente mais vide, elle renvoie `{}`. Le frontend traite `null` comme désactivé et toute autre valeur comme activée (sauf si `enabled: false` est défini).

Quand la section `blackbox` est présente, `blackbox.modules` est toujours renvoyée avec les quatre rôles, résolus depuis la config fusionnée avec les défauts. Un rôle configuré avec une liste vide est renvoyé comme liste vide, et le frontend ignore sa requête.

### GET /api/topology

Analyse et renvoie `topology.yaml`. Aplatit les `children` imbriqués en une liste plate de noeuds avec `parent` défini.

```bash
curl -s http://127.0.0.1:8001/api/topology | jq .nodes[0]
```

Renvoie `{"nodes":[], "networks":[], "zones":[], "tunnels":[], "links":[], "cameras":[]}` si le fichier n'existe pas.

### GET /api/layout

Renvoie les positions de noeuds sauvegardées et l'état des bascules d'affichage depuis `layout.json`. Renvoie `{}` si le fichier n'existe pas.

### POST /api/layout

Sauvegarde les positions de noeuds et l'état des bascules d'affichage. Le corps doit être un objet JSON.

```json
{
  "gw":              {"x": 120.5, "y": -45.2},
  "__srv__host1:9100": {"x": 300.0, "y": 80.0},
  "__zonesVisible":   true,
  "__tunnelsVisible": false,
  "__guestHidden":    {"vm-down": true}
}
```

| HTTP | Cause |
|---|---|
| 413 | Le payload dépasse 512 Ko |
| 422 | JSON invalide ou erreur de schéma |

### POST /api/reload

Valide `promlens.yaml` et `topology.yaml` avec `yaml.safe_load`. Aucun redémarrage du serveur n'est requis.

```bash
# Success
curl -s -X POST http://127.0.0.1:8001/api/reload | jq .
# {"ok": true}

# Parse error (HTTP 400)
curl -s -X POST http://127.0.0.1:8001/api/reload
# {"detail": "topology.yaml: mapping values are not allowed here\n  line 5, column 3"}
```

### GET /api/mtime

Renvoie la date de modification de `promlens.yaml` et `topology.yaml` en timestamps Unix. Utilise par le frontend quand [auto_reload](#option-auto_reload) est active, pour detecter un changement de fichier sans rien reanalyser.

Un fichier absent ou impossible a stat est renvoye a `0.0`. Pas de rate limiting : l'endpoint ne fait que deux appels `stat()`.

```bash
curl -s http://127.0.0.1:8001/api/mtime | jq .
```

```json
{
  "config": 1788470740.451429,
  "topology": 1788470741.299453
}
```

| Champ | Type | Description |
|---|---|---|
| `config` | float | Date de modification de `promlens.yaml` (`CONFIG_FILE`) |
| `topology` | float | Date de modification de `topology.yaml` (`TOPOLOGY_FILE`) |

Soumis a la meme authentification que les autres routes `/api/`.

### GET /api/alerts

Relaie `GET /api/v1/alerts` depuis Prometheus et renvoie le tableau d'objets d'alerte. Utilisé par le frontend pour alimenter le panneau d'alertes et les surcouches sur les noeuds. Indisponible en mode cert (le navigateur récupère alors les alertes directement depuis Prometheus).

Renvoie un tableau vide `[]` si Prometheus est injoignable ou renvoie une erreur. Ne propage pas les erreurs Prometheus à l'appelant.

**Réponse :**

```json
[
  {
    "labels":      {"alertname": "NodeHighCPU", "instance": "server1:9100", "severity": "warning"},
    "annotations": {"summary": "CPU above 85% for 10 minutes"},
    "state":       "firing",
    "activeAt":    "2026-05-25T10:00:00Z"
  }
]
```

**Erreurs :**

| HTTP | Cause |
|---|---|
| 429 | Limite de débit dépassée (partagée avec `POST /api/query`) |
| 503 | `promlens.yaml` introuvable ou champ `url` manquant |

```bash
curl -s http://127.0.0.1:8001/api/alerts | jq '[.[] | select(.state=="firing")]'
```

### GET /login

Renvoie le HTML de la page de connexion. N'a de sens que lorsque `app_auth.mode: basic`.

### POST /api/auth/login

Authentifie contre le fichier htpasswd. Pose un cookie de session HttpOnly en cas de succès.

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret"}'
```

### POST /api/auth/logout

Efface le cookie de session.

---

## 7. Internes du backend — sécurité

### Limitation de débit

`POST /api/query` est limité à **60 requêtes par minute et par IP client**. Implémenté avec une fenêtre glissante en mémoire (`collections.deque`). Les dépassements renvoient HTTP 429.

### Protection CSRF

Pour toutes les méthodes d'API non sûres (POST, PUT, DELETE, PATCH) sauf `/api/auth/*`, le backend compare l'en-tête `Origin` à l'en-tête `Host`. Une divergence renvoie HTTP 403.

```python
# Simplified logic
if origin and urlparse(origin).netloc != host:
    return 403
```

### Content-Security-Policy

La CSP est construite dynamiquement à partir de `promlens.yaml` et mise en cache jusqu'à la modification du fichier. Elle inclut l'origine Prometheus dans `connect-src` pour autoriser les requêtes directes en mode cert.

```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
font-src 'self' data:;
img-src 'self' data:;
connect-src 'self' https://prometheus.example.com;
```

### En-têtes de sécurité (toutes les réponses)

| En-tête | Valeur |
|---|---|
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | Dynamique (voir ci-dessus) |

### Taille maximale du fichier de layout

`POST /api/layout` rejette les payloads supérieurs à **512 Ko** avec HTTP 413.

---

## 8. Variables d'environnement

À définir dans `/etc/default/promlens` pour le paquet Debian, ou à passer au conteneur.

| Variable | Défaut | Description |
|---|---|---|
| `CONFIG_FILE` | `/etc/promlens/promlens.yaml` | Chemin du fichier de configuration Prometheus |
| `TOPOLOGY_FILE` | `/etc/promlens/topology.yaml` | Chemin du fichier de topologie |
| `LAYOUT_FILE` | `/var/lib/promlens/layout.json` | Chemin du fichier de persistance du layout |
| `BIND_HOST` | `127.0.0.1` | Adresse d'écoute |
| `BIND_PORT` | `8001` | Port d'écoute |
| `LOG_LEVEL` | `INFO` | Niveau de journalisation Python (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 9. Internes du frontend

### fetchAll — 20 requêtes parallèles

Chaque cycle de rafraîchissement exécute celles-ci en parallèle avec `Promise.all` :

| # | Source | Requête / Endpoint | Utilisé pour |
|---|---|---|---|
| 1 | Backend | `GET /api/topology` | Noeuds de topologie, zones, tunnels, liens, caméras |
| 2 | Prometheus | `up{exporter="node"}` | État up/down des noeuds |
| 3 | Prometheus | `rate(node_network_receive_bytes_total{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])` | RX par interface |
| 4 | Prometheus | `rate(node_network_transmit_bytes_total{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])` | TX par interface |
| 5 | Prometheus | `node_network_speed_bytes{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}` | Capacité d'interface |
| 6 | Prometheus | `node_uname_info` | Nom d'hôte (nodename), OS, version du noyau |
| 7 | Prometheus | `avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))` | CPU idle (inversé = utilisation) |
| 8 | Prometheus | `node_memory_MemAvailable_bytes` | RAM disponible |
| 9 | Prometheus | `node_memory_MemTotal_bytes` | RAM totale |
| 10 | Prometheus | `node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}` | Espace disque libre |
| 11 | Prometheus | `node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}` | Espace disque total |
| 12 | Prometheus | `node_load1` | Charge moyenne sur 1 minute |
| 13 | Prometheus | `node_boot_time_seconds` | Heure de démarrage (calcul de l'uptime) |
| 14 | Prometheus | `node_systemd_unit_state{state="failed"} == 1` | Units systemd en échec |
| 15 | Prometheus | `probe_success{module=~"<blackbox.modules.icmp>"}` | Résultats des sondes ICMP, blackbox uniquement (module par défaut : `icmp`) |
| 16 | Prometheus | `probe_success{module=~"<blackbox.modules.ssh>"}` | Résultats des sondes SSH, blackbox uniquement (module par défaut : `ssh_banner`) |
| 17 | Prometheus | `probe_success{module=~"<blackbox.modules.tcp>"}` | Résultats des sondes TCP connect, blackbox uniquement (module par défaut : `tcp_connect`) |
| 18 | Prometheus | `probe_success{module=~"<blackbox.modules.http>"}` | Résultats des sondes HTTP/HTTPS, blackbox uniquement (modules par défaut : `http_2xx`, `https_2xx`) |
| 19 | Prometheus | `libvirt_domain_info_state` | État des VM sur les hyperviseurs (libvirt uniquement) |
| 20 | Prometheus | `frigate_camera_fps` | État en ligne des caméras (frigate uniquement) |
| 21 | Backend / Prometheus | `GET /api/alerts` (relayé) ou `GET /api/v1/alerts` (mode cert, direct) | Alertes Prometheus actives |

Les requêtes 15 à 20 sont ignorées (la Promise se résout immédiatement) quand leur intégration est désactivée. Les requêtes 15 à 18 sont également ignorées quand leur rôle a une liste de modules vide dans [blackbox.modules](#blackboxmodules). La requête 21 s'exécute toujours ; elle renvoie un tableau vide en cas d'erreur, de sorte qu'une panne de l'alertmanager Prometheus ne bloque pas le graphe.

`GET /api/mtime` ne fait pas partie de ce lot : quand [auto_reload](#option-auto_reload) est active, il tourne sur son propre timer de 5 secondes et ne declenche un `fetchAll()` complet que si un fichier de configuration a change.

### Phases de buildGraph

**Phase 1 — Index des instances Prometheus**
Construit `promHostToSrvId` : nom d'hôte -> ID de noeud synthétique (`__srv__${instance}`). Détecte les noeuds fusionnés (instances Prometheus dont le nom d'hôte correspond à un ID de noeud de topologie).

**Phase 2 — Noeuds de topologie**
Crée tous les noeuds de topologie dans Vis.js avec des icônes SVG. Construit les liens parent-enfant. Calcule l'infobulle du lien (RX/TX) en utilisant l'interface déclarée si elle est présente.

**Phase 3 — Index de résolution des noms**
Construit la table `hostToNodeId` associant tous les noms connus (ID de topologie, IP, label, instance Prometheus, nodename issu de `node_uname_info`) aux ID de noeuds Vis.js. Calcule aussi `linkIfaceMap` et `tunnelIfaceExclude`.

**Phase 4 — Zones**
Résout récursivement les zones imbriquées. Collecte les membres de zone depuis les labels Prometheus et les listes `members` explicites. Trie les zones pour que les parents soient rendus avant les enfants.

**Phase 5 — Hôtes Prometheus**
Pour chaque instance de `up{exporter="node"}` :
- Calcule la couleur à partir du pire ratio de métrique.
- Si l'instance correspond à un noeud de topologie : met à jour l'icône et l'infobulle du noeud existant.
- Sinon : crée un nouveau noeud serveur et résout son parent (voir l'ordre de résolution du parent ci-dessous).
- Ajoute des liens intra-zone invisibles pour le regroupement physique.

**Phase 5b — Noeuds sondes uniquement**
Crée des noeuds légers pour les cibles qui apparaissent dans les sondes blackbox/TCP/HTTP mais n'ont pas d'instance `node_exporter`.

Une seconde passe rattache ensuite chacun de ces noeuds à celui désigné par son `parent_label`, une fois tous les noeuds sondes enregistrés afin qu'une cible de sonde puisse elle-même être le parent d'une autre. Sans parent résolvable, le noeud reste déconnecté et apparaît dans le panneau des orphelins -- ce qui était le cas de toutes les cibles TCP uniquement, la phase 7 ne dessinant des liens que pour les sondes ICMP et SSH.

**Phase 6 — Tunnels WireGuard**
Crée des liens en pointillés. Crée des noeuds fantômes pour les extrémités inconnues de Prometheus.

**Phase 7 — Liens de sondes blackbox**
Regroupe les sondes par paire de noeuds non ordonnée. Les sondes sont dédupliquées par cible réelle, donc un `destination_label` de groupe garde une entrée par sonde. Crée des liens colorés (vert=toutes UP, rouge=au moins une DOWN). Les sondes bidirectionnelles obtiennent des flèches aux deux extrémités.

Une sonde dont la source n'est pas résolue (pas de `source_label`, ou une valeur qui ne correspond à aucun noeud) part de `prometheus_node` si l'option est définie, sinon d'un noeud `prometheus` autonome ajouté au graphe. Une sonde dont la source et la destination pointent sur le même noeud ne crée pas de lien : elle reste dans le tooltip de ce noeud.

**Phase 8 — Caméras Frigate**
Crée un noeud par caméra à partir de `frigate_camera_fps`. Résout le parent via la section cameras, puis le label `parent`, puis le repli sur la topologie.

### Ordre de résolution du parent (hôtes Prometheus)

Priorité (de la plus haute à la plus basse) :

1. `promParentOverride` (interne — surcharges par membre explicite de zone)
2. Label `network` correspondant à un réseau déclaré par label ou par CIDR
3. `guest_label` valant une des `guest_values`, avec un label `parent_label`
   (par défaut : `job=vm` avec `parent`). Un noeud dont le parent est égal à sa
   propre instance est ignoré : une configuration qui étiquette chaque hôte avec
   `parent=<lui-meme>` pour l'inhibition d'alertes ne produit jamais de boucle.
4. Label `zone` correspondant à une zone déclarée
5. Correspondance CIDR dans `networks`
6. Repli : premier `router`, puis premier `switch` de la topologie

### Logique de couleur des noeuds

```
no data (unknown)  ->  grey   #3d4560
down               ->  red    #f04f4f
metricMax >= 0.9   ->  red    #f04f4f
metricMax >= 0.7   ->  orange #e0972a
  OR failed systemd units
  OR failing TCP probe
  OR failing HTTP probe
  OR failing blackbox probe
otherwise          ->  green  #2dba6e

metricMax = max(cpuRatio, memRatio, diskRatio)
cpuRatio   = 1 - cpuIdle
memRatio   = 1 - memAvail / memTotal
diskRatio  = 1 - diskAvail / diskTotal
```

Si `metricMax >= 1.0`, le noeud clignote avec une animation d'anneau rouge via `requestAnimationFrame`.

**Surcouche de sévérité d'alerte.** Une fois la couleur de base calculée, si au moins une alerte Prometheus est active pour le noeud (correspondance via `instance_label`), la couleur de l'icône et du libellé du noeud est remplacée par la couleur de sévérité. La couleur de base reste calculée et affichée dans l'infobulle ; seule la représentation visuelle change.

| Sévérité | Couleur |
|---|---|
| `critical` | rouge `#f04f4f` |
| `warning` | orange `#e0972a` |
| `info` | bleu `#4a8adf` |

Quand un noeud porte des alertes de sévérités différentes, la sévérité la plus prioritaire l'emporte : critical > warning > info.

### Panneau des alertes Prometheus

Un panneau flottant apparaît dans le coin inférieur droit du graphe dès qu'au moins une alerte Prometheus est active. Il est replié par défaut ; cliquez sur l'en-tête pour déployer la liste des alertes.

**La couleur de la bordure et du badge du panneau** reflète la pire sévérité parmi toutes les alertes actuellement actives :

| Pire sévérité | Couleur de bordure / badge |
|---|---|
| `critical` | rouge `#f04f4f` |
| `warning` | orange `#e0972a` |
| `info` | bleu `#4a8adf` |

**Les lignes d'alerte** affichent le nom de l'alerte (coloré selon sa propre sévérité) et le noeud auquel elle appartient. Le noeud est identifié via l'option de configuration `instance_label` (défaut : `instance`), de sorte que la valeur affichée corresponde à l'identifiant de noeud utilisé dans le graphe. L'annotation `summary` complète est affichée sous le nom de l'alerte quand elle est présente.

### Informations d'alerte dans l'infobulle et la fenêtre de détail

Quand une alerte Prometheus est active pour un noeud :

- **Infobulle au survol** : affiche les noms d'alerte colorés par sévérité, sans texte de résumé. Le résumé est omis pour éviter le débordement de l'infobulle compacte.
- **Fenêtre modale au double-clic** : ouvre la fenêtre de détail qui affiche les noms d'alerte ainsi que l'annotation `summary` complète.

### Point clignotant sur les noeuds en alerte

Les noeuds ayant au moins une alerte Prometheus active affichent un petit cercle plein dessiné sur un canvas de surcouche dédié (`#alertDotCanvas`) positionné au-dessus du canvas Vis.js. La couleur du point correspond à la pire sévérité des alertes actives du noeud (même priorité : critical > warning > info).

Le point clignote à environ 1 Hz via `requestAnimationFrame`. Comme le canvas de surcouche est indépendant de Vis.js, le dessin des points ne déclenche aucun redessin Vis.js et n'a aucun impact sur les performances de rendu du graphe.

### Page des alertes Prometheus

Un double-clic sur l'en-tête du panneau **Prometheus Alerts** ouvre une vue pleine page dédiée listant toutes les alertes actuellement actives.

Pour revenir au graphe, cliquez sur le bouton **←** de l'en-tête ou appuyez sur `Escape`.

**Mise en page :**

- Les alertes sont triées par sévérité (critical → warning → info), puis par ordre alphabétique du nom d'alerte.
- Chaque alerte est rendue sous forme de carte affichant : badge de sévérité, nom de l'alerte et valeur de l'instance.
- Cliquer sur une carte la déploie pour afficher :
  - L'annotation `summary` complète (si présente).
  - Tous les labels restants sous forme de pastilles `cle=valeur` (hors `alertname` et `severity`, affichés dans l'en-tête de la carte).
  - Toutes les annotations restantes sous forme de pastilles `cle=valeur` (hors `summary`, affichée au-dessus).

### Logique de couleur des liens

```
worst = max(ifaceRatio) across all visible interfaces
worst >= 0.9  ->  red    #f04f4f
worst >= 0.7  ->  orange #e0972a
node is up    ->  green  rgba(45,186,110,.3)
node is down  ->  red    rgba(240,79,79,.35)  (dashed)

ifaceRatio = max(rx, tx) / speed
             speed defaults to 1 Gbps if absent or <= 0
```

### Contrôles de l'interface

| Contrôle | Action |
|---|---|
| RELOAD | Appelle `POST /api/reload` puis `fetchAll()` |
| TEST | Appelle `fetchAll()` |
| Rafraîchir (flèche) | Appelle `fetchAll()` |
| Fit (carré) | `network.fit()` avec animation |
| Reset | Efface `savedPositions` et le localStorage, puis `fetchAll()` |
| Save (grille) | Appelle `POST /api/layout` avec les positions et l'état des bascules courants |
| Mode sélection | Active la sélection par rectangle élastique (glisser pour sélectionner plusieurs noeuds) |
| Plein écran | Passe en plein écran ; affiche une barre d'outils flottante |
| Menu VIEWS | Bascule les zones, les tunnels, les liens ICMP-UP, les liens SSH-UP, la légende, et la visibilité des invités par type et par état |
| Intervalle de rafraîchissement | 10s / 30s / 1m / 5m / off |
| Recherche | Cible et fait clignoter un noeud ou une zone par libellé ou par ID (appuyez sur `/` pour placer le focus dans le champ) |
| Fenêtre de recherche | `Ctrl+F` / `Cmd+F` ouvre une recherche de type palette de commandes sur les noeuds et zones visibles |
| Fenêtre d'aide | `Ctrl+H` / `Cmd+H` ouvre la liste des raccourcis clavier |

**Clic sur un noeud :** un simple clic met le noeud en avant (masque les liens non liés). Un double-clic ouvre la fenêtre de détail. Si `camera_url` est configuré, cliquer sur un noeud caméra Frigate ouvre `camera_url/#camera_name` dans une nouvelle fenêtre.

**Clic sur un lien :**
- Lien blackbox : ouvre le graphe Prometheus de `probe_success{...="..."}` sur la cible réelle de la sonde, en utilisant le label depuis lequel cette cible a été lue : `destination_label` normalement, `instance_label` (défaut `instance`) quand `destination_label` est un label de groupe.
- Tunnel WireGuard : ouvre le graphe Prometheus des RX/TX de l'interface WireGuard.
- Lien serveur normal : ouvre le graphe Prometheus des RX/TX de l'instance serveur.

#### Raccourcis clavier

Six raccourcis globaux utilisent `Ctrl` sur Linux/Windows et `Cmd` sur macOS. Tous remplacent le comportement par défaut du navigateur.

| Raccourci | Action | Page des alertes |
|---|---|---|
| `Ctrl+F` | Ouvre la fenêtre de recherche | ignoré |
| `Ctrl+S` | Sauvegarde le layout | ignoré |
| `Ctrl+M` | Bascule le mode sélection | ignoré |
| `Ctrl+A` | Ajuste la vue (fit) | ignoré |
| `Ctrl+R` | Rafraîchit immédiatement | actif |
| `Ctrl+H` | Ouvre la fenêtre d'aide des raccourcis clavier | actif |

Chaque raccourci déclenche le bouton correspondant de la barre d'outils (`saveLayoutBtn`, `selectModeBtn`, `fitBtn`, `refreshBtn`) : un raccourci ne peut donc pas diverger du comportement de son bouton.

Toutes les associations sont définies dans une unique table `SHORTCUTS` dans `src/static/index.html`. Cette table alimente à la fois le gestionnaire de touches et la fenêtre d'aide : la liste affichée correspond donc toujours aux associations réelles.

**Exceptions :**

- `Ctrl+A` n'est pas intercepté tant qu'un champ de saisie a le focus. Il y conserve son sens natif « tout sélectionner ».
- `Ctrl+F`, `Ctrl+S`, `Ctrl+M` et `Ctrl+A` sont marqués `mapOnly` et sont ignorés tant que la page des alertes Prometheus est ouverte. `Ctrl+R` et `Ctrl+H` fonctionnent partout.

**Fenêtre d'aide :**

`Ctrl+H` ouvre `<dialog id="helpModal">`, intitulée « Keyboard shortcuts ». Elle liste toutes les entrées de la table `SHORTCUTS`, plus deux lignes pour les touches gérées par leurs propres écouteurs :

| Touche | Action |
|---|---|
| `/` | Place le focus dans le champ de recherche du bandeau |
| `esc` | Ferme une fenêtre, ou annule la mise en avant du noeud |

Échap ferme la fenêtre d'aide, sans annuler en plus la mise en avant du noeud.

La fenêtre de recherche et la fenêtre d'aide ne se superposent jamais : ouvrir l'une ferme l'autre.

#### Fenêtre de recherche

`Ctrl+F` (`Cmd+F` sur macOS) ouvre `<dialog id="searchModal">` à la place de la barre de recherche native du navigateur. Le raccourci est ignoré tant que la page des alertes Prometheus est ouverte.

La fenêtre contient un champ de saisie, une liste de résultats filtrée en direct (30 entrées maximum) et une ligne d'aide en pied. Chaque ligne affiche le libellé de l'entrée et son badge de type (`server`, `vm`, `lxc`, `router`, `zone`, ...).

**Entrées cherchables**, dans cet ordre :

1. Tous les noeuds actuellement affichés.
2. Toutes les zones racines actuellement affichées.

Les noeuds masqués par les bascules de visibilité des invités et les zones masquées par la bascule des zones ne sont pas cherchables. Seules les zones racines sont dessinées : les zones imbriquées sont repliées dans la bulle de leur zone racine et ne sont donc pas cherchables séparément.

**Rangs de correspondance**, du meilleur au moins bon :

| Rang | Condition |
|---|---|
| 0 | Le libellé est égal à la requête |
| 1 | Le libellé commence par la requête |
| 2 | Le libellé contient la requête |
| 3 | L'ID du noeud ou de la zone contient la requête |

Le tri est stable : à rang égal, les noeuds restent devant les zones.

**Touches dans la fenêtre :**

| Touche | Action |
|---|---|
| Haut / Bas | Déplace la sélection dans la liste de résultats |
| Entrée | Zoome sur l'entrée sélectionnée |
| Échap | Ferme la fenêtre |
| `Ctrl+F` | Resélectionne le texte du champ |

Cliquer sur une ligne la sélectionne également.

**Comportement du zoom :**

- Noeud : `network.focus()` à l'échelle 1.5, plus un anneau cyan clignotant autour du noeud pendant 1,5 s. Résultat identique à celui de la barre de recherche du bandeau.
- Zone : la vue se centre sur la bulle de zone et zoome pour que la bulle tienne dans le canvas avec une petite marge, échelle plafonnée à 1.5, puis le contour de la bulle clignote en cyan pendant 1,5 s.

La barre de recherche du bandeau (et son raccourci `/`) correspond désormais aussi aux zones, avec le même classement. Y appuyer sur Entrée zoome sur la meilleure correspondance, noeud ou zone.

#### Bascules de visibilité des invités

Neuf bascules du menu VIEWS masquent les noeuds invités par type et par état. Toutes sont visibles (cochées) par défaut. Désactiver une bascule masque les noeuds correspondants et tous les liens qui les touchent.

| Type | `type` de topologie / valeur de `guest_label` | Bascules |
|---|---|---|
| vm | `vm` | VM UNMONITORED / VM DOWN / VM UP |
| lxc | `lxc` | LXC UNMONITORED / LXC DOWN / LXC UP |
| pod | `kube` | POD UNMONITORED / POD DOWN / POD UP |

Les noeuds de tout autre type (`cloud`, `router`, `firewall`, `switch`, `server`, `zigbee`, `wifi`) n'ont pas de type invité et ne sont jamais affectés par ces bascules.

Le type invité est déduit du `type` de topologie pour les noeuds déclarés dans `topology.yaml`, et de la valeur de `guest_label` pour les hôtes découverts dans Prometheus. Avec `guest_label: role`, une cible étiquetée `role: vm` obtient donc le type `vm` et suit les bascules VM, tandis que `role: host` n'a pas de type invité.

L'état est résolu dans `buildGraph` :

- Chaque noeud de topologie démarre à `unmonitored`.
- Phase 5 : un noeud fusionné avec une instance Prometheus `up` passe à `up` (`up == 1`) ou `down` (`up == 0`). Une valeur non numérique le laisse à `unmonitored`.
- Phase 5c : les noeuds VM créés depuis l'exporteur libvirt (VM sans `node_exporter` propre) reçoivent toujours le type `vm` et restent toujours à `unmonitored`, quel que soit l'état rapporté par libvirt. Une telle VM n'est pas réellement supervisée, elle ne doit donc jamais compter comme up ou down. Sa couleur, son icône et son infobulle affichent toujours l'état libvirt réel (running, paused, shut off, crashed). En conséquence, VM DOWN et VM UP ne s'appliquent qu'aux VM déclarées dans `topology.yaml` et fusionnées avec une série Prometheus `up` : une VM est `up` seulement si elle est déclarée dans la topologie *et* qu'une série `up` lui correspond avec la valeur `1`.

L'état des bascules est sauvegardé par `POST /api/layout` sous la clé `__guestHidden`, un objet associant `"<kind>-<state>"` (par exemple `"vm-down"`) à un booléen signifiant *masqué*. Les clés historiques `__libvirtShutOffHidden` et `__libvirtUpHidden` sont ignorées au chargement d'un layout plus ancien.

### Paramètres physiques Vis.js

Solveur : `forceAtlas2Based`

| Paramètre | Valeur |
|---|---|
| `gravitationalConstant` | -65 |
| `centralGravity` | 0.004 |
| `springLength` | 130 |
| `springConstant` | 0.08 |
| `damping` | 0.42 |
| `avoidOverlap` | 0.6 |
| `stabilization.iterations` | 160 |

La physique est désactivée après stabilisation. Les rafraîchissements suivants mettent à jour les jeux de données de noeuds et de liens sur place, sans relancer le moteur physique.

---

## 10. Exemples complets de topology.yaml

### Site unique minimal

```yaml
nodes:
  - id: internet
    label: Internet
    type: cloud

  - id: gw
    label: Gateway
    type: router
    parent: internet
    interface: ppp0

networks:
  - cidr: 192.168.1.0/24
    gateway: gw
    label: LAN
```

Tous les hôtes Prometheus ayant une IP dans `192.168.1.0/24` se rattachent automatiquement à `gw`.

### Avec zones et enfants imbriqués

```yaml
nodes:
  - id: isp-cloud
    label: ISP cloud
    type: cloud

  - id: gw
    label: Gateway
    type: router
    parent: isp-cloud
    interface: wan0
    children:
      - id: sw-prod
        type: switch
        interface: eth-prod
      - id: sw-cam
        type: switch
        interface: eth-cam

zones:
  - id: prod
    label: Production
    parent: sw-prod

  - id: vms
    label: VMs
    parent: prod
    default_parent: sw-prod

networks:
  - cidr: 10.0.1.0/24
    gateway: sw-prod
```

Les hôtes Prometheus portant `zone=prod` apparaissent dans la bulle Production. Ceux portant `zone=vms` apparaissent dans la bulle VMs (imbriquée dans prod).

### Avec tunnels WireGuard et filtrage d'interfaces

```yaml
nodes:
  - id: gw
    label: Gateway
    type: router
    parent: cloud

links:
  - from: gw
    to: [server1, server2]
    interface: eth0

tunnels:
  - from: gw
    to:
      - remote1
      - remote2
    interface: wg0
    parent_interface: ppp0
```

Résultat :
- Les liens `gw <-> server1` et `gw <-> server2` n'affichent que `eth0` dans l'infobulle.
- Les liens vers `remote1` et `remote2` sont en pointillés avec les métriques de `wg0`.
- `wg0` est masqué des infobulles de liens normaux sur `gw`.
- La vitesse de `ppp0` est utilisée comme capacité du lien WireGuard.

### Avec caméras Frigate et sondes blackbox

```yaml
# promlens.yaml
frigate:
  enabled: true
  camera_url: https://frigate.example.com

blackbox:
  enabled: true
  destination_label: instance
  source_label: probe_src
```

```yaml
# topology.yaml
nodes:
  - id: sw-cam
    label: Camera Switch
    type: switch
    parent: gw

cameras:
  - name: front-door
    parent: sw-cam
  - name: backyard
    parent: sw-cam
```

Les caméras `front-door` et `backyard` apparaissent comme des noeuds rattachés à `sw-cam`. Cliquer sur l'une ou l'autre ouvre `https://frigate.example.com/#front-door` (ou `#backyard`) dans une fenêtre popup.

---

## 11. Limitations et pièges

- **Mode cert et CORS** : en mode cert (`auth.type: cert`), le navigateur interroge Prometheus directement. Prometheus doit être configuré avec `--web.cors.origin` correspondant exactement à l'origine de ProMLens. Une origine générique (`*`) ne fonctionnera pas avec `direct_credentials: true`, car les navigateurs rejettent les requêtes avec identifiants vers des origines génériques.

- **La valeur par défaut de direct_credentials est false** : depuis le commit `97a0ac6`, les requêtes directes en mode cert partent sans identifiants par défaut (`credentials: 'omit'`). Ne mettez `direct_credentials: true` que si votre Prometheus exige le certificat client pour chaque requête.

- **La limite de débit est par IP, en mémoire** : la limite de 60 req/min est remise à zéro au redémarrage du serveur et n'est pas partagée entre plusieurs instances. Le frontend émet jusqu'à 20 requêtes parallèles par cycle de rafraîchissement : un intervalle de 30 secondes génère donc au plus 40 requêtes par minute (20 par cycle, deux cycles par minute).

- **Le fichier de layout doit être inscriptible par le processus** : le processus qui écrit `layout.json` a besoin d'un accès en écriture sur `LAYOUT_FILE`. Le paquet Debian met en place les permissions adaptées. Dans un conteneur, montez le répertoire parent avec un accès en écriture.

- **Webhooks en mode cert** : en mode cert (`auth.type: cert`), le backend n'a pas de certificat client, donc le notifier interroge les métriques sans identifiants. Si Prometheus impose mTLS, le sondage échoue à chaque cycle et se contente de journaliser un avertissement. Les notifications webhook ne sont donc utilisables que lorsque le backend lui-même peut joindre Prometheus (mode `none`, `basic` ou `bearer`).

- **Aucune notification pour les alertes déjà actives au démarrage** : le premier cycle de sondage initialise l'état en silence. Un redémarrage pendant un incident ne renvoie pas de notifications pour les conditions déjà actives, et leur éventuel événement `resolved` est tout de même envoyé.

- **Une métrique instable produit quand même des paires d'événements** : `for_cycles` ne retarde que le côté firing. Une métrique qui reste au-dessus de son seuil rouge pendant `for_cycles` sondages, puis redescend pendant un sondage, puis remonte, produit `firing` / `resolved` / `firing`. Augmentez `for_cycles` ou le seuil pour un noeud réellement bruyant.

- **Les alertes notifiées et les alertes affichées sont deux flux différents** : le graphe, le panneau d'alertes et la page des alertes affichent les alertes Prometheus de `/api/v1/alerts` ; les webhooks notifient les alertes ProMLens calculées à partir des seuils. Une règle d'alerte Prometheus ne déclenche jamais de webhook, et une alerte ProMLens n'apparaît jamais dans le panneau d'alertes.

- **La livraison des webhooks est en best effort** : un POST en échec est journalisé puis abandonné, sans réessai ni file d'attente. Les alertes qui se déclenchent et se résolvent dans une même fenêtre d'`interval` ne sont jamais notifiées.

- **Le rechargement YAML ne fait qu'analyser la syntaxe** : `POST /api/reload` valide la syntaxe avec `yaml.safe_load` mais ne valide ni les valeurs ni les types des champs. Un champ `url` invalide passe la validation de rechargement mais provoque un HTTP 503 à la requête suivante.

- **auto_reload ne surveille que la mtime** : la detection compare la date de modification renvoyee par `GET /api/mtime`. Un fichier reecrit avec une mtime inchangee n'est pas detecte, et le polling ajoute une requete toutes les 5 secondes par onglet ouvert. Comme le declencheur est un rafraichissement complet, la meme reserve que ci-dessus s'applique : une configuration qui s'analyse mais qui est semantiquement fausse est rechargee comme une autre.
