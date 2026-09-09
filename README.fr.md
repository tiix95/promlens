# ProMLens

[English](README.md) | **Francais**

Visualiseur de topologie reseau qui superpose les metriques Prometheus temps reel sur un graphe Vis.js interactif.

Version : **0.21.0**

## Captures d'ecran

Toutes les captures ci-dessous tournent sur la configuration d'exemple livree dans `examples/data/`.

### Vue d'ensemble de la topologie

Noeuds de topologie statiques, hotes Prometheus rattaches par zone ou par hyperviseur, tunnels
WireGuard, sondes blackbox et cameras Frigate sur une seule carte.

![Vue d'ensemble](docs/screenshots/overview.png)

### Metriques d'un noeud

Le survol d'un noeud affiche son OS, l'usage CPU / RAM / disque, la charge, l'uptime, les unites
systemd en echec, le resultat des sondes et -- sur un hyperviseur -- l'etat de chaque domaine libvirt.

![Infobulle d'un noeud](docs/screenshots/node-tooltip.png)

### Debit des interfaces

Le survol d'un lien affiche le RX/TX par interface aux deux extremites. Les liens passent en orange
a 70% et en rouge a 90% de la vitesse de l'interface.

![Infobulle d'un lien](docs/screenshots/link-tooltip.png)

### Menu VIEWS

Affiche ou masque les zones, les tunnels, les liens de sonde, la legende, et cache les noeuds
invites (VM / LXC / POD) par etat : non supervise, down, up.

![Menu VIEWS](docs/screenshots/views-menu.png)

### Panneaux lateraux

Les alertes Prometheus, les alertes calculees par ProMLens, tous les liens dessines et les noeuds
sans parent structurel.

![Panneaux lateraux](docs/screenshots/panels.png)

### Page des alertes Prometheus

Les alertes groupees par noeud et triees par severite, depliables pour afficher labels et annotations.

![Page des alertes Prometheus](docs/screenshots/alerts-page.png)

### Reproduire la demo

`examples/mock_prometheus.py` est un faux Prometheus sans dependance (stdlib uniquement) qui sert un
jeu de donnees de demonstration correspondant a `examples/data/topology.yaml` -- aucun Prometheus reel
n'est necessaire.

```bash
# 1. Servir le jeu de donnees de demo sur http://127.0.0.1:9090
python3 examples/mock_prometheus.py &

# 2. Faire pointer une copie de la configuration d'exemple dessus
mkdir -p /tmp/promlens-demo
cp examples/data/topology.yaml examples/data/layout.json /tmp/promlens-demo/
sed 's|^url: .*|url: http://127.0.0.1:9090|' examples/data/promlens.yaml > /tmp/promlens-demo/promlens.yaml

# 3. Lancer ProMLens dessus, puis ouvrir http://127.0.0.1:8000
./run.sh /tmp/promlens-demo/promlens.yaml /tmp/promlens-demo/topology.yaml /tmp/promlens-demo/layout.json
```

## Demarrage rapide

### Conteneur (recommande pour le developpement)

```bash
# Build et lancement -- monte examples/data/ pour les fichiers de configuration
make run

# Ouvrir http://127.0.0.1:8001
```

### Paquet Debian (recommande pour la production)

```bash
# Construire le .deb
make deb

# Installer
dpkg -i promlens_*.deb

# Editer les fichiers de configuration
vim /etc/promlens/promlens.yaml
vim /etc/promlens/topology.yaml

# Demarrer le service
systemctl start promlens
```

Les fichiers de configuration vont dans `/etc/promlens/`. L'etat du layout est persiste dans `/var/lib/promlens/layout.json`.

## promlens.yaml -- tous les champs

```yaml
url: https://prometheus.example.com       # obligatoire

auth:
  type: none                              # none | basic | bearer | cert
  username: admin                         # basic uniquement
  password: secret                        # basic uniquement
  token: mytoken                          # bearer uniquement

instance_label: instance                  # label utilise comme identifiant de noeud (defaut: instance)
parent_label: parent                      # label portant le nom du noeud parent (defaut: parent)
guest_label: role                         # label marquant un noeud comme invite (defaut: job)
guest_values: [vm, lxc]                   # valeurs de guest_label traitees comme invite (defaut: [vm])
direct_credentials: false                 # mode cert uniquement : envoie le certificat client sur les requetes directes
ssl_verify: true
timeout: 30                               # timeout HTTP en secondes
proxy: http://proxy.example.com:8080      # optionnel
refresh: 30                               # intervalle d'auto-refresh par defaut en secondes (defaut: 30)
auto_reload: false                        # reconstruit le graphe des qu'un fichier de config change (defaut: false)

app_auth:
  mode: none                              # none | basic | cert
  htpasswd: /etc/promlens/.htpasswd       # mode basic : chemin du fichier htpasswd
  secret: "change-me-random-string"       # cle de signature des sessions
  session_days: 7

libvirt:                                  # supprimer la section ou mettre enabled: false pour desactiver
  enabled: true
  instance_label: instance

blackbox:                                 # supprimer la section ou mettre enabled: false pour desactiver
  enabled: true
  destination_label: instance             # label identifiant la cible de la sonde (defaut: instance)
  source_label: job                       # label identifiant la source de la sonde (optionnel)
  prometheus_node: mon01                  # noeud qui heberge Prometheus : source des sondes
                                          # dont la source n'est pas resolue (optionnel)
  http_node_label: upstream               # label pour les sondes HTTP/HTTPS (defaut: upstream)
  # modules:                              # noms des modules blackbox interroges, par role
  #   icmp: [icmp]                        # aretes du graphe (defaut: icmp)
  #   ssh:  [ssh_banner]                  # aretes du graphe (defaut: ssh_banner)
  #   tcp:  [tcp_connect]                 # tooltip du noeud (defaut: tcp_connect)
  #   http: [http_2xx, https_2xx]         # tooltip du noeud (defaut: http_2xx, https_2xx)
  #   # une chaine seule est acceptee, une liste vide desactive le role
  dest_aliases:                           # alias -> nom de noeud pour les cibles non resolvables
    mynode:                               # prioritaire sur toute autre resolution de nom
      - alias-1                           # alias exact : la sonde est rattachee a mynode
      - alias-2                           # alias prefixe : "alias-2-server" resout le noeud "server"
                                          # les tooltips nomment la sonde par l'alias, pas par mynode
                                          # un mynode inconnu est signale dans le panneau des warnings
  source_aliases:                         # idem, pour les valeurs de source_label
    mynode:                               # prioritaire sur toute autre resolution de nom
      - alias-3                           # la sonde part de mynode au lieu de prometheus_node
      - alias-4                           # les tooltips nomment la source par l'alias, pas par mynode
                                          # un mynode inconnu est signale dans le panneau des warnings

frigate:                                  # supprimer la section ou mettre enabled: false pour desactiver
  enabled: true
  camera_url: https://frigate.example.com  # clic sur une camera -> ouvre camera_url/#camera_name

webhooks:                                 # supprimer la section ou mettre enabled: false pour desactiver
  enabled: true
  interval: 60                            # intervalle de poll en secondes (defaut: 60, minimum: 10)
  for_cycles: 2                           # cycles consecutifs avant de notifier (defaut: 2)
  notify_resolved: true                   # notifier aussi la disparition d'une alerte (defaut: true)
  link: https://promlens.example.com/     # valeur du champ "url" du payload
  severities: [critical]                  # ne notifier que ces severites (defaut: toutes)
  disabled:                               # motifs glob sur alertname a ne jamais notifier
    - SystemdUnitFailed
    - "*High"
  ca_file: /etc/promlens/webhook-ca.crt   # CA optionnelle pour verifier le certificat TLS du webhook
  targets:
    - url: https://notify.example.com/api/notify
      topic: infra
      recipients: [ops, oncall]
      module: promlens
      headers:
        Authorization: "Bearer ${NOTIFY_TOKEN}"   # $VAR / ${VAR} sont expanses
```

## topology.yaml -- resume des sections

| Section | Role |
|---|---|
| `nodes` | Noeuds d'infrastructure statiques (router, switch, server, cloud, firewall, vm, lxc, kube, zigbee, wifi) |
| `networks` | Plages CIDR qui rattachent automatiquement les hosts Prometheus a un noeud gateway |
| `zones` | Bulles de regroupement visuel pilotees par le label Prometheus `zone` |
| `tunnels` | Tunnels WireGuard affiches en liens pointilles avec leurs metriques d'interface dediees |
| `links` | Declarations de filtrage d'interface -- limite les interfaces affichees dans les tooltips de liens |
| `cameras` | Force le noeud parent de cameras Frigate specifiques, par nom |

Reference complete et exemples : voir [DOC.fr.md](DOC.fr.md)

## Endpoints d'API

| Methode | Chemin | Description |
|---|---|---|
| `GET` | `/api/config` | URL Prometheus, type d'authentification, integrations activees |
| `POST` | `/api/query` | Proxy d'une requete PromQL instant ou range |
| `GET` | `/api/topology` | topology.yaml parse |
| `GET` | `/api/layout` | Positions de noeuds et etats d'affichage sauvegardes |
| `POST` | `/api/layout` | Sauvegarde les positions de noeuds et les etats d'affichage |
| `GET` | `/api/mtime` | Dates de modification des deux fichiers de configuration |
| `POST` | `/api/reload` | Valide et recharge les deux fichiers de configuration |
| `GET` | `/login` | Page de login (mode basic uniquement) |
| `POST` | `/api/auth/login` | Authentifie et renvoie un cookie de session |
| `POST` | `/api/auth/logout` | Efface le cookie de session |

## Modes d'authentification (connexion Prometheus)

| Mode | Fonctionnement |
|---|---|
| `none` | Aucune authentification |
| `basic` | Le backend ajoute l'entete `Authorization: Basic` |
| `bearer` | Le backend ajoute l'entete `Authorization: Bearer` |
| `cert` | Le navigateur interroge Prometheus directement ; le certificat client est gere par le navigateur |

## Fonctionnalites

- Graphe interactif -- noeuds deplacables, zoom/pan, layout force-directed
- Metriques temps reel -- CPU, RAM, disque, load, uptime dans les tooltips
- Coloration par seuil -- vert / orange (>=70%) / rouge (>=90%), clignotement a 100%
- Liens reseau -- RX/TX par interface, colores selon le taux d'utilisation
- Tunnels WireGuard -- liens pointilles avec leurs metriques d'interface dediees
- Bulles de zone -- regroupement visuel par label Prometheus `zone`
- Sondes blackbox -- liens de sonde ICMP, SSH, TCP connect, HTTP/HTTPS et etat par noeud ; noms des modules configurables via `blackbox.modules` ; plusieurs sondes peuvent etre groupees sur un noeud via `destination_label`, chacune listee avec sa cible
- Cameras Frigate -- un noeud par camera, vert=en ligne, rouge=hors ligne
- VMs libvirt -- liste des VMs avec leur etat dans les tooltips de l'hyperviseur
- Visibilite des invites -- masquer les noeuds VM / LXC / POD par etat (unmonitored, down, up) depuis le menu VIEWS
- Fenetre de recherche -- `Ctrl+F` (`Cmd+F` sur macOS) ouvre une palette de commandes sur les noeuds et zones visibles, fleches pour naviguer, Entree pour zoomer
- Raccourcis clavier -- combinaisons `Ctrl`/`Cmd` pour la recherche, la sauvegarde du layout, le mode selection, l'ajustement de la vue et le rafraichissement ; `Ctrl+H` les liste dans une fenetre d'aide
- Layout persistant -- positions et etats d'affichage sauvegardes cote serveur
- Webhooks d'alerte -- notifications cote serveur pour les alertes calculees par ProMLens lui-meme (seuils, noeud down, unites systemd en echec), aucune regle d'alerting Prometheus requise
- Rechargement de configuration -- le bouton RELOAD valide et recharge a chaud les deux fichiers YAML
- Rechargement automatique -- avec `auto_reload: true`, modifier `promlens.yaml` ou `topology.yaml` reconstruit le graphe en moins de 5 secondes, sans attendre l'intervalle de rafraichissement
- Securite -- rate limiting, protection CSRF, entetes CSP

## Licence

MIT -- voir [LICENSE](LICENSE).

Les ressources tierces embarquees (JetBrains Mono, Syne, vis-network) conservent leurs propres licences :
voir [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
