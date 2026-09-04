# ProMLens

[English](README.md) | **Francais**

Visualiseur de topologie reseau qui superpose les metriques Prometheus temps reel sur un graphe Vis.js interactif.

Version : **0.15.0**

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
direct_credentials: false                 # mode cert uniquement : envoie le certificat client sur les requetes directes
ssl_verify: true
timeout: 30                               # timeout HTTP en secondes
proxy: http://proxy.example.com:8080      # optionnel
refresh: 30                               # intervalle d'auto-refresh par defaut en secondes (defaut: 30)

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
  http_node_label: upstream               # label pour les sondes HTTP/HTTPS (defaut: upstream)
  dest_aliases:                           # alias -> nom de noeud pour les cibles non resolvables
    mynode:
      - alias-1
      - alias-2

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
| `nodes` | Noeuds d'infrastructure statiques (router, switch, cloud, firewall, vm, lxc, kube, zigbee, wifi) |
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
- Sondes blackbox -- liens de sonde ICMP, SSH, TCP connect, HTTP/HTTPS et etat par noeud
- Cameras Frigate -- un noeud par camera, vert=en ligne, rouge=hors ligne
- VMs libvirt -- liste des VMs avec leur etat dans les tooltips de l'hyperviseur
- Visibilite des invites -- masquer les noeuds VM / LXC / POD par etat (unmonitored, down, up) depuis le menu VIEWS
- Layout persistant -- positions et etats d'affichage sauvegardes cote serveur
- Webhooks d'alerte -- notifications cote serveur pour les alertes calculees par ProMLens lui-meme (seuils, noeud down, unites systemd en echec), aucune regle d'alerting Prometheus requise
- Rechargement de configuration -- le bouton RELOAD valide et recharge a chaud les deux fichiers YAML
- Securite -- rate limiting, protection CSRF, entetes CSP

## Licence

MIT -- voir [LICENSE](LICENSE).

Les ressources tierces embarquees (JetBrains Mono, Syne, vis-network) conservent leurs propres licences :
voir [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
