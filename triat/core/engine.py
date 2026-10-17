"""Moteur : orchestre file d'attente, extraction, lecture, SponsorBlock,
bibliothèque et recommandations.

C'est la seule façade dont l'UI a besoin. Tout le travail bloquant part
dans des threads ; tous les signaux sont émis sur le thread principal.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import parse_qs, urlsplit

from gi.repository import GLib, GObject

from .auth import CookieManager
from .extractor import VIDEO_HEIGHTS, ExtractionError, Extractor
from .innertube import InnerTube
from .downloads import DownloadManager
from .equalizer import clamp_gains
from .library import Library
from .lyrics import LyricsService
from .models import Segment, Track, TrackRef, source_of
from .player import Player
from .queue import Queue
from .recommend import RADIO_CONTEXT, Recommender
from .reco import is_blocked
from . import search_results
from .scrobble import Scrobbler
from .settings import Settings
from .soundcloud import SoundCloudClient
from .sources import SourceRouter, unplayable_label, unplayable_reason
from .sponsorblock import ASK, SKIP, SkipPlanner, SponsorBlockClient
from .sync import SyncServer
from .ytmusic import YTMusicService

log = logging.getLogger(__name__)

# En dessous de ce seuil, « précédent » rembobine au lieu de changer de piste.
RESTART_THRESHOLD = 3.0
# Marge avant la fin d'une piste pour déclencher la radio automatique.
AUTOPLAY_REFILL = 2
# Une URL résolue d'avance plus vieille que cela (ou proche de son `expire`)
# est jetée : googlevideo la refuserait.
PREFETCH_MAX_AGE = 3600.0
EXPIRY_MARGIN = 600.0
# Au-delà de cette position, une piste est considérée comme réellement lue.
FAILURE_RESET_POSITION = 5.0
# Un « suivant » avant cette part de la piste compte comme un saut (signal
# négatif des recommandations ; l'historique, lui, ne change pas).
SKIP_RATIO = 0.3
# Après une radio vide, on ne réessaie pas avant ce délai (secondes) : le
# réseau peut revenir pendant une longue piste.
AUTOPLAY_RETRY = 60.0

# Minuterie de sommeil : durée du fondu final avant la pause, et cadence de
# vérification (l'échéance se lit sur `Engine._clock`, injectable).
SLEEP_FADE_SECONDS = 30.0
SLEEP_TICK_MS = 250

# Normalisation par piste : cible et bornes du gain appliqué.
LOUDNESS_TARGET_LUFS = -14.0
YOUTUBE_REFERENCE_LUFS = -14.0      # référence de `loudnessDb` chez YouTube
GAIN_MIN_DB = -12.0
GAIN_MAX_DB = 6.0


def normalisation_gain(loudness_db) -> float:
    """Gain (dB) qui ramène une piste à la cible de -14 LUFS.

    `loudness_db` est le `playerConfig.audioConfig.loudnessDb` de YouTube,
    exprimé par rapport à sa référence (-14 LUFS) : valeur positive = piste
    plus forte que la référence. NON VÉRIFIÉ : ce champ n'apparaît dans
    aucune fixture ni dans les champs yt-dlp actuels de `Track` ; absent ou
    illisible -> gain 0, aucune requête réseau supplémentaire.
    """
    try:
        value = float(loudness_db)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    gain = LOUDNESS_TARGET_LUFS - (YOUTUBE_REFERENCE_LUFS + value)
    return max(GAIN_MIN_DB, min(GAIN_MAX_DB, gain))


def _expires_soon(url: str) -> bool:
    """True si l'URL googlevideo expire dans moins de dix minutes."""
    try:
        query = parse_qs(urlsplit(url).query)
        values = query.get("expire") or query.get("expires")
        return bool(values) and int(values[0]) - time.time() < EXPIRY_MARGIN
    except (ValueError, TypeError):
        return False


class Engine(GObject.Object):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "position-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "duration-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "track-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "busy": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Segment, secondes gagnées
        "segment-skipped": (GObject.SignalFlags.RUN_FIRST, None, (object, float)),
        # Segment dont la politique est « demander »
        "segment-pending": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Clip de l'écran Immersion : "off" | "loading" | "ready" |
        # "unavailable", et le VideoStream quand il est prêt.
        "video-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        # Métadonnées de la bibliothèque complétées en arrière-plan
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Volume, aléatoire ou répétition modifiés (interface, MPRIS, touches) :
        # chaque vue et MPRIS se resynchronisent sur ce signal.
        "options-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Segments SponsorBlock de la piste courante (liste, éventuellement
        # vide) : prêts, ou remis à zéro au changement de piste.
        "segments-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # « Tout effacer » terminé : les caches d'interface doivent se vider.
        "data-wiped": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Minuterie de sommeil : dict d'état ({"mode", "minutes", "deadline",
        # "fading"}) ou None quand elle est annulée ou terminée.
        "sleep-timer-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, settings: Settings | None = None,
                 library: Library | None = None,
                 player=None, extractor=None, ytmusic=None,
                 recommender=None, sponsorblock=None, cookies=None,
                 innertube=None) -> None:
        """Les dépendances sont injectables pour les tests ; par défaut
        l'Engine construit la chaîne réelle."""
        super().__init__()
        self.settings = settings or Settings()
        # Bilan SponsorBlock depuis le lancement (Réglages › SponsorBlock).
        self.session_skips = {"seconds": 0.0, "segments": 0, "tracks": set()}
        self.state = "idle"             # dernier état du lecteur
        self.library = library or Library()
        self.cookies = cookies or CookieManager(self.settings)
        if extractor is None:
            # Routeur de sources : YouTube inchangé, SoundCloud seulement si
            # le réglage est actif (aucun réseau à la construction).
            extractor = SourceRouter(
                Extractor(self.cookies),
                SoundCloudClient() if self.settings.get("soundcloud_enabled")
                else None,
                self.settings)
        self.extractor = extractor
        self.ytmusic = ytmusic or YTMusicService(self.cookies)
        self.innertube = innertube or InnerTube(self.cookies, self.settings)
        self.recommender = recommender or Recommender(
            self.library, self.ytmusic, self.extractor, self.innertube,
            is_private=lambda: bool(self.settings.get("private_session")))
        # Session privée : ni paroles ni segments ne s'écrivent en bibliothèque.
        def is_private() -> bool:
            return bool(self.settings.get("private_session"))

        self.sponsorblock = sponsorblock or SponsorBlockClient(
            self.library, is_private=is_private)
        self.lyrics = LyricsService(self.library, is_private=is_private)
        self.scrobbler = Scrobbler(self.settings)
        self.scrobbler.preload()          # jeton en mémoire, hors du fil GTK
        self.downloads = DownloadManager(self, self.settings, self.library,
                                         self.sponsorblock)
        self.downloads.attach_cookies(self.cookies)
        self.player = player or Player()
        self.queue = Queue()
        # Serveur de synchro Wi-Fi avec le téléphone. Il ne démarre pas ici :
        # l'app le lance si « sync_enabled » (voir set_sync_enabled). Le
        # cookie est lu à la demande, seulement si l'utilisateur le partage.
        self.sync = SyncServer(
            self.library, self.settings,
            cookie_header=lambda: self.cookies.cookie_header(),
            # Appelé depuis le fil du serveur : on republie sur le fil GTK.
            on_changed=lambda _changes: GLib.idle_add(
                lambda: (self.emit("library-changed"), False)[1]))

        self._planner: SkipPlanner | None = None
        self._pending_asked: set[str] = set()
        self._load_token = 0
        self._current_ref: TrackRef | None = None
        self._max_position = 0.0
        # Instant (epoch) où la piste a réellement démarré : ListenBrainz
        # date l'écoute à son début.
        self._started_at: float | None = None
        self._recorded = False
        self._refilling = False
        self._busy = False
        # Requêtes en vol qui justifient « occupé » : l'indicateur ne retombe
        # que lorsqu'elles sont toutes terminées.
        self._busy_count = 0
        # Jeton de la radio : une réponse tardive d'une autre file est ignorée.
        self._radio_token = 0
        self._autosave_id = 0
        self._sleep: dict | None = None
        self._sleep_id = 0
        self._sleep_fade_base = 0.0
        self._sleep_fade_start = 0.0
        # URL de flux déjà résolue pour la piste suivante, afin d'enchaîner
        # sans le temps mort d'un appel réseau.
        self._prefetched: tuple[str, Track, float] | None = None
        self._prefetching = False
        # Identifiant de la piste réellement confiée au lecteur : tant que la
        # nouvelle n'est pas résolue, l'ancienne joue encore et ses
        # événements ne doivent pas être crédités à la nouvelle.
        self._playing_id: str | None = None
        self._retried_token = -1
        # Échecs consécutifs sans qu'une piste ait réellement joué : borne
        # l'enchaînement automatique quand tout est illisible.
        self._failures = 0
        # Position restaurée au lancement, consommée une seule fois par
        # `resume` (l'autosauvegarde ne doit pas la rejouer plus tard).
        self._restore_position = 0.0
        self._autoplay_failed_for: str | None = None
        self._autoplay_retry_at = 0.0
        self._clock = time.monotonic      # remplaçable dans les tests
        # Pistes ajoutées par la radio (et non par l'utilisateur) : un saut
        # y est noté « radio », et « ne plus recommander » peut les retirer.
        self._radio_ids: set[str] = set()
        # Raison de la dernière radio, pour « Lecture en cours ».
        self.radio_reason = ""
        self._video_wanted = False
        self._video_state = "off"

        self.queue.repeat = self.settings.get("repeat")
        self.queue._shuffle = bool(self.settings.get("shuffle"))
        self.player.set_volume(float(self.settings.get("volume")))
        self.player.set_normalisation(bool(self.settings.get("normalise_volume")))
        self.player.pause_fade = bool(self.settings.get("pause_fade"))
        self._apply_equalizer()

        # Une session qui s'éteint sans prévenir (déconnexion, SIGKILL) ne
        # doit pas faire perdre la file ni la position.
        self._autosave_id = GLib.timeout_add_seconds(30, self._autosave)

        # Sauvegarde automatique quotidienne : seulement sur le vrai profil
        # (aucune dépendance injectée), jamais dans les tests.
        if library is None and settings is None:
            threading.Thread(target=self._auto_backup, daemon=True).start()

        self.queue.connect("current-changed", self._on_queue_current)
        self.queue.connect("changed", self._on_queue_changed)
        self.player.connect("state-changed", self._on_player_state)
        self.player.connect("position-changed", self._on_player_position)
        self.player.connect("duration-changed",
                            lambda _p, v: self.emit("duration-changed", v))
        self.player.connect("eof", self._on_player_eof)
        self.player.connect("error", self._on_player_error)
        self.player.connect("video-error", lambda _p, _m:
                            self._set_video_state("unavailable"))

    def _autosave(self) -> bool:
        if self.state == "playing":
            try:
                self.save_state()
            except Exception as exc:
                log.debug("Sauvegarde périodique : %s", exc)
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _auto_backup() -> None:
        try:
            from . import backup
            backup.maybe_auto_backup()
        except Exception as exc:
            log.debug("Sauvegarde automatique impossible : %s", exc)

    def set_soundcloud_enabled(self, enabled: bool) -> None:
        """Active ou coupe SoundCloud : retient le choix et (re)crée ou
        retire le client du routeur de sources."""
        self.settings.set("soundcloud_enabled", bool(enabled))
        self.settings.save()
        router = self.extractor
        if isinstance(router, SourceRouter):
            # Le réglage est posé avant : le getter rend le client existant.
            router.soundcloud = (router.soundcloud or SoundCloudClient()
                                 if enabled else None)

    @property
    def soundcloud(self):
        """Client SoundCloud, ou None (désactivé, ou extracteur injecté
        qui n'est pas un SourceRouter)."""
        router = self.extractor
        return router.soundcloud if isinstance(router, SourceRouter) else None

    def _on_queue_changed(self, _queue) -> None:
        # L'anticipation ne vaut plus si la suite a changé.
        upcoming = self.queue.upcoming
        if not upcoming or (self._prefetched is not None
                            and self._prefetched[0] != upcoming[0].video_id):
            self._prefetched = None
        self.emit("queue-changed")

    def set_normalisation(self, enabled: bool) -> None:
        self.settings.set("normalise_volume", enabled)
        self.settings.save()
        self.player.set_normalisation(enabled)
        # Le gain par piste suit le même interrupteur « Égaliser le volume ».
        track = self.player.track
        if track is not None:
            self._apply_track_gain(track)

    def set_pause_fade(self, enabled: bool) -> None:
        """Fondu d'environ 200 ms à la pause et à la reprise."""
        self.settings.set("pause_fade", bool(enabled))
        self.settings.save()
        self.player.pause_fade = bool(enabled)

    def _apply_track_gain(self, track: Track) -> None:
        gain = 0.0
        if (self.settings.get("normalise_volume") and not track.is_local
                and source_of(track.video_id) == "youtube"):
            gain = normalisation_gain(getattr(track, "loudness_db", None))
        set_gain = getattr(self.player, "set_gain", None)
        if set_gain is not None:
            set_gain(gain)

    # ---- Minuterie de sommeil --------------------------------------------

    def set_sleep_timer(self, minutes: int | None = None, *,
                        end_of_track: bool = False,
                        end_of_queue: bool = False) -> None:
        """Arme la minuterie : dans `minutes` minutes (fondu de 30 s puis
        pause), à la fin de la piste ou à la fin de la file. Un seul mode ;
        remplace la minuterie précédente."""
        modes = sum([bool(minutes), bool(end_of_track), bool(end_of_queue)])
        if modes != 1 or (minutes is not None and minutes <= 0):
            raise ValueError("une seule minuterie : minutes, fin de piste "
                             "ou fin de file")
        self._clear_sleep(restore=True)
        mode = "end_of_track" if end_of_track else (
            "end_of_queue" if end_of_queue else "minutes")
        self._sleep = {
            "mode": mode,
            "minutes": int(minutes) if minutes else None,
            "deadline": self._clock() + minutes * 60 if minutes else None,
            "fading": False,
        }
        if mode == "minutes":
            self._sleep_id = GLib.timeout_add(SLEEP_TICK_MS, self._sleep_tick)
        self.emit("sleep-timer-changed", dict(self._sleep))

    def cancel_sleep_timer(self) -> None:
        had = self._sleep is not None
        self._clear_sleep(restore=True)
        if had:
            self.emit("sleep-timer-changed", None)

    def sleep_timer_remaining(self) -> float | None:
        """Secondes avant l'échéance (0 pendant le fondu) ; None hors mode
        « minutes » ou sans minuterie."""
        sleep = self._sleep
        if sleep is None or sleep["deadline"] is None:
            return None
        return max(0.0, sleep["deadline"] - self._clock())

    def _clear_sleep(self, restore: bool) -> None:
        if self._sleep_id:
            GLib.source_remove(self._sleep_id)
            self._sleep_id = 0
        if restore and self._sleep is not None and self._sleep["fading"]:
            self.player.set_volume(self._sleep_fade_base)
        self._sleep = None

    def _sleep_tick(self) -> bool:
        sleep = self._sleep
        if sleep is None:
            self._sleep_id = 0
            return GLib.SOURCE_REMOVE
        now = self._clock()
        if not sleep["fading"]:
            if now < sleep["deadline"]:
                return GLib.SOURCE_CONTINUE
            sleep["fading"] = True
            self._sleep_fade_start = now
            self._sleep_fade_base = float(self.settings.get("volume"))
            self.emit("sleep-timer-changed", dict(sleep))
        frac = min(1.0, (now - self._sleep_fade_start) / SLEEP_FADE_SECONDS)
        if frac < 1.0:
            self.player.set_volume(self._sleep_fade_base * (1.0 - frac))
            return GLib.SOURCE_CONTINUE
        # Fondu terminé : pause, puis volume rétabli pour la prochaine lecture.
        self._sleep_id = 0
        if self.player.state == "playing":
            self.player.toggle_pause()
        self.player.set_volume(self._sleep_fade_base)
        self._sleep = None
        self.emit("sleep-timer-changed", None)
        return GLib.SOURCE_REMOVE

    def _sleep_mode(self) -> str | None:
        return self._sleep["mode"] if self._sleep else None

    def _finish_sleep(self) -> None:
        self._clear_sleep(restore=False)
        self.emit("sleep-timer-changed", None)

    def set_sync_enabled(self, on: bool) -> bool:
        """Active ou coupe la synchro Wi-Fi, et retient le choix.
        Renvoie True si le serveur écoute après l'appel."""
        self.settings.set("sync_enabled", bool(on))
        self.settings.save()
        if on:
            return self.sync.start()
        self.sync.stop()
        return False

    @property
    def equalizer(self) -> tuple[bool, tuple[float, ...]]:
        return (bool(self.settings.get("eq_enabled")),
                clamp_gains(self.settings.get("eq_gains")))

    def set_equalizer(self, enabled: bool | None = None,
                      gains=None) -> None:
        """Change l'état et/ou les gains, sauvegarde, applique en direct."""
        if enabled is not None:
            self.settings.set("eq_enabled", bool(enabled))
        if gains is not None:
            self.settings.set("eq_gains", list(clamp_gains(gains)))
        self.settings.save()
        self._apply_equalizer()

    def _apply_equalizer(self) -> None:
        enabled, gains = self.equalizer
        self.player.set_equalizer(gains if enabled else None)

    # ---- Clip vidéo (écran Immersion) -----------------------------------

    @property
    def video_state(self) -> str:
        return self._video_state

    def set_video_mode(self, enabled: bool) -> None:
        """Active le clip pour la piste en cours et toutes les suivantes."""
        if enabled == self._video_wanted:
            return
        self._video_wanted = enabled
        if not enabled:
            self.player.clear_video()
            self._set_video_state("off")
        elif self._current_ref is not None and self.player.state != "idle":
            self._load_video(self._current_ref, self._load_token)

    def _load_video(self, ref: TrackRef, token: int) -> None:
        if ref.source != "youtube":
            self._set_video_state("unavailable")     # pas de clip SoundCloud
            return
        self._set_video_state("loading")
        height = VIDEO_HEIGHTS.get(self.settings.get("audio_quality"), 1080)

        def worker() -> None:
            try:
                stream = self.extractor.resolve_video(ref.url, height)
            except Exception as exc:
                log.debug("Clip indisponible pour %s : %s", ref.video_id, exc)
                stream = None
            GLib.idle_add(self._video_ready, ref.video_id, stream, token)

        threading.Thread(target=worker, daemon=True).start()

    def _video_ready(self, video_id: str, stream, token: int) -> bool:
        if token != self._load_token or not self._video_wanted:
            return GLib.SOURCE_REMOVE
        if stream is None:
            self._set_video_state("unavailable")
            return GLib.SOURCE_REMOVE
        self.player.set_video(video_id, stream.url)
        self._set_video_state("ready", stream)
        return GLib.SOURCE_REMOVE

    def _set_video_state(self, state: str, stream=None) -> None:
        self._video_state = state
        self.emit("video-changed", state, stream)

    # ---- Commandes de lecture ------------------------------------------

    def play_query(self, query: str) -> None:
        """Recherche puis lecture. Retourne immédiatement."""
        query = query.strip()
        if not query:
            return
        self._set_busy(True)
        threading.Thread(target=self._search_worker, args=(query,),
                         daemon=True).start()

    def _search_worker(self, query: str) -> None:
        try:
            # Une URL directe se joue telle quelle, sans passer par la recherche.
            if query.startswith(("http://", "https://")):
                track = self.extractor.resolve(query)
                refs = [track.to_ref()]
            else:
                refs = self.recommender.search(query, limit=25)
        except ExtractionError as exc:
            GLib.idle_add(self._fail, str(exc))
            return
        except Exception as exc:
            log.exception("Recherche impossible")
            GLib.idle_add(self._fail, str(exc))
            return
        if not refs:
            GLib.idle_add(self._fail, "Aucun résultat")
            return
        GLib.idle_add(self._start_with, refs)

    def _start_with(self, refs: list[TrackRef]) -> bool:
        self._set_busy(False)
        self.play_refs(refs, start=0)
        return GLib.SOURCE_REMOVE

    def start_radio(self) -> None:
        """Relance une radio à partir de la dernière piste connue."""
        ref = self._current_ref or (self.library.recent(limit=1) or [None])[0]
        if ref is None:
            self.emit("error", "Écoutez d'abord une piste pour lancer une radio")
            return
        # `_try_autoplay` part de la piste courante : on la pose si la file
        # est vide mais que l'historique nous donne un point de départ.
        if self._current_ref is None:
            self._current_ref = ref
        self._try_autoplay()

    def search_async(self, query: str, callback) -> None:
        """Recherche en arrière-plan. `callback(refs, erreur)` sur le thread UI."""
        query = query.strip()
        if not query:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                if query.startswith(("http://", "https://")):
                    refs = [self.extractor.resolve(query).to_ref()]
                else:
                    refs = self.recommender.search(query, limit=40)
            except Exception as exc:
                message = str(exc).strip().splitlines()[0] if str(exc) else "Erreur"
                GLib.idle_add(self._search_done, callback, [], message)
                return
            GLib.idle_add(self._search_done, callback, refs, None)

        threading.Thread(target=worker, daemon=True).start()

    def search_all_async(self, query: str, callback) -> None:
        """Recherche complète : pistes YouTube, correspondances locales,
        artistes et playlists. `callback(SearchResults, erreur)`."""
        query = query.strip()
        if not query:
            return
        self._set_busy(True)

        def worker() -> None:
            local = self.library.search(query, limit=20)
            playlists = self.library.list_playlists()
            error = None
            try:
                if query.startswith(("http://", "https://")):
                    remote = [self.extractor.resolve(query).to_ref()]
                else:
                    remote = self.recommender.search(query, limit=40)
            except Exception as exc:
                remote = []
                error = str(exc).strip().splitlines()[0] if str(exc) else "Erreur"
            results = search_results.build(query, remote, local, playlists)
            # Hors ligne, les pistes locales suffisent : pas d'erreur à montrer.
            if results.tracks:
                error = None
            GLib.idle_add(self._search_done, callback, results, error)

        threading.Thread(target=worker, daemon=True).start()

    def _search_done(self, callback, refs, error) -> bool:
        self._set_busy(False)
        callback(refs, error)
        return GLib.SOURCE_REMOVE

    def _cache_tracks(self, refs) -> None:
        """Mémorise les pistes en file dans la bibliothèque, sauf en session
        privée : la recherche locale révélerait ce qui a été lu."""
        if not self.settings.get("private_session"):
            self.library.upsert_many(refs)

    def play_refs(self, refs, start: int = 0) -> None:
        """Remplace la file et lance la lecture."""
        self._cache_tracks(refs)
        self._failures = 0
        self.queue.set_items(refs, start=start)

    def enqueue(self, refs, next_up: bool = False) -> None:
        self._cache_tracks(refs)
        if next_up:
            self.queue.insert_next(refs)
        else:
            self.queue.append(refs)

    def toggle_pause(self) -> None:
        # Piste restaurée au démarrage mais pas encore ouverte : « lecture »
        # doit la lancer (touche média, barre, bouton Reprendre), pas
        # basculer la pause d'un lecteur vide.
        if self.player.state == "idle" and self.queue.current is not None:
            self.resume()
            return
        self.player.toggle_pause()

    def stop(self) -> None:
        self._flush_history(completed=False)
        self.player.stop()
        self._reset_segments()
        self._current_ref = None

    def next(self) -> None:
        self._failures = 0
        self._note_skip()
        self._flush_history(completed=False)
        if self.queue.next(manual=True) is None:
            self._try_autoplay()

    def previous(self) -> None:
        # Un « précédent » en cours de piste rembobine, comme partout ailleurs.
        if (self._current_position() > RESTART_THRESHOLD
                and self.player.state != "idle"):
            self.player.seek(0.0)
            return
        self._failures = 0
        self._flush_history(completed=False)
        self.queue.previous()

    def jump_to(self, index: int) -> None:
        self._failures = 0
        self._flush_history(completed=False)
        self.queue.jump_to(index)

    def seek(self, seconds: float) -> None:
        self.player.seek(seconds)

    def set_volume(self, value: float) -> None:
        self.player.set_volume(value)
        self.settings.set("volume", value)
        self.emit("options-changed")

    @property
    def shuffle(self) -> bool:
        return self.queue.shuffle

    @shuffle.setter
    def shuffle(self, value: bool) -> None:
        self.queue.shuffle = value
        self.settings.set("shuffle", value)
        self.emit("options-changed")

    @property
    def repeat(self) -> str:
        return self.queue.repeat

    @repeat.setter
    def repeat(self, mode: str) -> None:
        self.queue.repeat = mode
        self.settings.set("repeat", mode)
        self.emit("options-changed")

    @property
    def current(self) -> TrackRef | None:
        return self._current_ref

    # ---- Favoris --------------------------------------------------------

    def toggle_favorite(self) -> bool:
        if self._current_ref is None:
            return False
        return self.library.toggle_favorite(self._current_ref)

    def is_favorite(self) -> bool:
        return (self._current_ref is not None
                and self.library.is_favorite(self._current_ref.video_id))

    # ---- SponsorBlock ---------------------------------------------------

    def skip_segment(self, segment: Segment) -> None:
        """Saut demandé explicitement (politique « demander »)."""
        self._perform_skip(segment)

    def cancel_segment(self, segment: Segment) -> None:
        """L'utilisateur annule : revenir au début et ne plus proposer."""
        if self._planner is not None:
            self._planner.disable(segment)
        self.player.seek(segment.start)

    # ---- Chargement d'une piste -----------------------------------------

    def _on_queue_current(self, _queue: Queue, ref: TrackRef | None) -> None:
        self._flush_history(completed=False)
        if ref is None:
            self.player.stop()
            self._current_ref = None
            self.emit("track-changed", None)
            return

        self._load_token += 1
        token = self._load_token
        self._current_ref = ref
        self._reset_segments()
        self._pending_asked.clear()
        self._max_position = 0.0
        self._started_at = None
        self._recorded = False
        self._playing_id = None
        self._restore_position = 0.0
        self._autoplay_failed_for = None
        # Lu ici, sur le fil principal : le fil de résolution ne touche
        # jamais à `_prefetched`.
        anticipated = self._take_prefetched(ref)

        # L'UI affiche la piste immédiatement, avant même la résolution réseau.
        self.emit("track-changed", ref)
        self._set_busy(True)
        threading.Thread(target=self._resolve_worker,
                         args=(ref, token, anticipated), daemon=True).start()
        threading.Thread(target=self._segments_worker, args=(ref, token),
                         daemon=True).start()

    def _take_prefetched(self, ref: TrackRef) -> Track | None:
        """Récupère la résolution anticipée de cette piste, si elle existe
        et si son URL n'a pas expiré."""
        if self._prefetched is None or self._prefetched[0] != ref.video_id:
            return None
        _vid, track, at = self._prefetched
        self._prefetched = None
        if time.monotonic() - at > PREFETCH_MAX_AGE or _expires_soon(track.stream_url):
            return None
        return track

    def _prefetch_next(self) -> None:
        """Résout la piste suivante pendant que la courante joue."""
        if self._prefetching:
            return
        upcoming = self.queue.upcoming
        if not upcoming:
            return
        target = upcoming[0]
        if self._prefetched is not None and self._prefetched[0] == target.video_id:
            return
        if self.downloads.local_path(target) is not None:
            return          # déjà hors ligne, rien à anticiper
        self._prefetching = True

        def worker() -> None:
            try:
                track = self.extractor.resolve(target.url)
            except Exception as exc:
                log.debug("Préchargement impossible : %s", exc)
                track = None
            GLib.idle_add(self._prefetch_done, target.video_id, track)

        threading.Thread(target=worker, daemon=True).start()

    def _prefetch_done(self, video_id: str, track) -> bool:
        self._prefetching = False
        if track is not None:
            self._prefetched = (video_id, track, time.monotonic())
        return GLib.SOURCE_REMOVE

    def _resolve_worker(self, ref: TrackRef, token: int,
                        anticipated: Track | None = None) -> None:
        # Piste déjà résolue par anticipation : on enchaîne sans attendre.
        if anticipated is not None:
            GLib.idle_add(self._resolve_done, anticipated, token)
            return
        # Un fichier téléchargé évite la résolution réseau et fonctionne
        # hors ligne.
        local = self.downloads.local_path(ref)
        if local is not None:
            GLib.idle_add(self._resolve_done, Track(
                video_id=ref.video_id, title=ref.title, artist=ref.artist,
                duration=ref.duration, stream_url=local.as_uri(),
                webpage_url=ref.url, thumbnail=ref.thumbnail,
                album=ref.album, is_local=True), token)
            return
        try:
            track = self.extractor.resolve(ref.url)
        except Exception as exc:
            GLib.idle_add(self._resolve_failed, ref, token, str(exc),
                          unplayable_reason(exc))
            return
        GLib.idle_add(self._resolve_done, track, token)

    def _resolve_done(self, track: Track, token: int, start: float = 0.0) -> bool:
        # La requête est terminée, périmée ou non : elle ne compte plus.
        self._set_busy(False)
        # Une piste plus récente a été demandée pendant la résolution.
        if token != self._load_token:
            return GLib.SOURCE_REMOVE
        # yt-dlp connaît mieux les métadonnées que la recherche : on affine.
        enriched = track.to_ref()
        if self._current_ref is not None:
            previous = self._current_ref
            if not enriched.thumbnail:
                enriched.thumbnail = previous.thumbnail
            self._current_ref = enriched
            if not self.settings.get("private_session"):
                self.library.upsert_track(enriched)
            # La piste a déjà été annoncée avant la résolution : on ne
            # réémet que si yt-dlp a réellement corrigé quelque chose.
            if enriched != previous:
                self.emit("track-changed", enriched)
        self._playing_id = track.video_id
        if self._started_at is None:
            self._started_at = time.time()
        self._apply_track_gain(track)
        self.player.play_track(track, start=start)
        if self._video_wanted:
            self._load_video(track.to_ref(), token)
        if not self.settings.get("private_session"):
            try:
                self.scrobbler.now_playing(enriched if self._current_ref else None)
            except Exception as exc:
                log.debug("Signalement d'écoute impossible : %s", exc)
        # La suivante se prépare pendant que celle-ci joue.
        GLib.timeout_add_seconds(3, lambda: (self._prefetch_next(), False)[1])
        return GLib.SOURCE_REMOVE

    def _resolve_failed(self, ref: TrackRef, token: int, message: str,
                        reason: str | None = None) -> bool:
        self._set_busy(False)
        if token != self._load_token:
            return GLib.SOURCE_REMOVE
        first_line = message.strip().splitlines()[0] if message.strip() else message
        if reason:
            # Piste SoundCloud non lisible : libellé précis, puis la suivante.
            self.emit("error", f"« {ref.title} » : {unplayable_label(reason)}")
        else:
            self.emit("error", f"« {ref.title} » illisible : {first_line}")
        # Une piste morte ne doit pas bloquer la file.
        self._advance_after_failure(token)
        return GLib.SOURCE_REMOVE

    def _advance_after_failure(self, token: int) -> bool:
        """Passe à la suite après un échec, sans jamais boucler à l'infini.

        Renvoie False quand on renonce : trop d'échecs d'affilée (aucune
        piste n'a joué entre-temps) ou plus de suivante. L'avance est
        manuelle pour que « répéter une piste » ne rejoue pas la morte.
        """
        self._failures += 1
        limit = max(3, min(len(self.queue.items), 10))
        if self._failures >= limit:
            self.emit("error", "Lecture interrompue : plusieurs pistes de "
                               "suite sont illisibles")
            return False
        if not self.queue.has_next:
            return False
        GLib.timeout_add(600, lambda: (
            token == self._load_token and self.queue.next(manual=True),
            False)[1])
        return True

    def current_segments(self) -> list:
        """Segments SponsorBlock de la piste courante (vide si aucun)."""
        planner = self._planner
        return list(getattr(planner, "segments", []) or []) if planner else []

    def _reset_segments(self) -> None:
        had = self._planner is not None
        self._planner = None
        if had:
            self.emit("segments-changed", [])

    def _segments_worker(self, ref: TrackRef, token: int) -> None:
        if ref.source != "youtube":
            return          # SponsorBlock ne connaît que YouTube
        if not self.settings.get("sponsorblock_enabled"):
            return
        policies = self.settings.get("sponsorblock_categories") or {}
        wanted = [cat for cat, action in policies.items() if action != "ignore"]
        if not wanted:
            return
        segments = self.sponsorblock.segments_for(ref.video_id, wanted, ref.duration)
        GLib.idle_add(self._segments_ready, segments, policies, token)

    def _segments_ready(self, segments, policies: dict, token: int) -> bool:
        if token != self._load_token:
            return GLib.SOURCE_REMOVE
        self._planner = SkipPlanner(segments, policies)
        self.emit("segments-changed", list(segments or []))
        if segments:
            log.debug("SponsorBlock : %d segment(s) pour %s",
                     len(segments), self._current_ref.video_id if self._current_ref else "?")
        return GLib.SOURCE_REMOVE

    # ---- Réactions au lecteur -------------------------------------------

    def _on_player_state(self, _player: Player, state: str) -> None:
        self.state = state
        self.emit("state-changed", state)

    def _event_is_current(self) -> bool:
        ref = self._current_ref
        return ref is not None and self._playing_id == ref.video_id

    def _current_position(self) -> float:
        """Position de la piste courante, 0 tant que sa résolution est en
        cours (le lecteur joue alors encore l'ancienne)."""
        return self.player.position_hint if self._event_is_current() else 0.0

    def _on_player_position(self, _player: Player, position: float) -> None:
        if not self._event_is_current():
            return                  # reliquat de la piste précédente
        if position > FAILURE_RESET_POSITION:
            self._failures = 0      # la lecture est réellement établie
        self._max_position = max(self._max_position, position)
        self.emit("position-changed", position)

        if self._planner is not None:
            segment = self._planner.segment_at(position)
            if segment is not None:
                policy = self._planner.policy_for(segment.category)
                if policy == SKIP:
                    self._perform_skip(segment)
                elif policy == ASK:
                    key = segment.uuid or f"{segment.category}:{segment.start}"
                    if key not in self._pending_asked:
                        self._pending_asked.add(key)
                        self.emit("segment-pending", segment)

        # Préparer la suite avant la fin, pour enchaîner sans trou.
        if (self.settings.get("autoplay_radio")
                and not self.queue.has_next
                and len(self.queue.upcoming) < AUTOPLAY_REFILL
                and self.player.duration > 0
                and position > self.player.duration - 30):
            self._try_autoplay()

    def _perform_skip(self, segment: Segment) -> None:
        if self._planner is None:
            return
        target = self._planner.target_for(segment, self.player.duration)
        gained = max(0.0, target - self.player.position_hint)
        self._planner.disable(segment)          # ne pas resauter le même passage
        self._planner.note_skip(gained)
        self.session_skips["seconds"] += gained
        self.session_skips["segments"] += 1
        if self._current_ref is not None:
            self.session_skips["tracks"].add(self._current_ref.video_id)
        self.player.seek(target)
        self.emit("segment-skipped", segment, gained)

    def _on_player_eof(self, _player: Player) -> None:
        if not self._event_is_current():
            return
        self._failures = 0
        self._flush_history(completed=True)
        if self._sleep_mode() == "end_of_track":
            self._finish_sleep()        # la piste est finie : on s'arrête là
            return
        if self.queue.next() is None:
            if self._sleep_mode() == "end_of_queue":
                self._finish_sleep()
            else:
                self._try_autoplay()

    def _on_player_error(self, _player: Player, message: str) -> None:
        if not self._event_is_current():
            return
        ref = self._current_ref
        track = self.player.track
        # Un flux expiré ou refusé se résout une seconde fois, à la position
        # atteinte, avant de renoncer à la piste.
        if (ref is not None and track is not None and not track.is_local
                and self._retried_token != self._load_token):
            self._retried_token = self._load_token
            self._reresolve(ref, self._load_token, self.player.position_hint)
            return
        self.emit("error", message)
        self._advance_after_failure(self._load_token)

    def _reresolve(self, ref: TrackRef, token: int, start: float) -> None:
        self._playing_id = None
        self._set_busy(True)

        def worker() -> None:
            try:
                track = self.extractor.resolve(ref.url)
            except Exception as exc:
                GLib.idle_add(self._resolve_failed, ref, token, str(exc),
                              unplayable_reason(exc))
                return
            GLib.idle_add(self._resolve_done, track, token, start)

        threading.Thread(target=worker, daemon=True).start()

    # ---- Radio automatique ----------------------------------------------

    def _try_autoplay(self) -> None:
        if not self.settings.get("autoplay_radio") or self._refilling:
            return
        ref = self._current_ref
        if self._sleep_mode() == "end_of_queue":
            return          # la file doit se terminer, pas se prolonger
        if ref is None or ref.source != "youtube":
            return          # pas de radio depuis SoundCloud : la file s'arrête
        if (ref.video_id == self._autoplay_failed_for
                and self._clock() < self._autoplay_retry_at):
            return
        self._refilling = True
        self._radio_token += 1
        # Tout ce qui vient de l'état partagé est lu ici, sur le fil principal.
        items = self.queue.items
        exclude = [t.video_id for t in items]
        context = items[-RADIO_CONTEXT:]
        private = bool(self.settings.get("private_session"))
        threading.Thread(
            target=self._autoplay_worker,
            args=(ref, self._radio_token, exclude, context, private),
            daemon=True).start()

    def _autoplay_worker(self, ref: TrackRef, token: int, exclude: list,
                         context: list, private: bool) -> None:
        reason = ""
        try:
            refs = self.recommender.radio_for(
                ref, limit=20, exclude=exclude, context=context,
                private=private)
            reason = getattr(self.recommender, "last_reason", "")
        except Exception as exc:
            log.warning("Radio automatique impossible: %s", exc)
            refs = []
        GLib.idle_add(self._autoplay_ready, refs, token, ref.video_id, reason)

    def _autoplay_ready(self, refs, token: int | None = None,
                        origin: str | None = None, reason: str = "") -> bool:
        if token is not None and token != self._radio_token:
            return GLib.SOURCE_REMOVE        # une autre radio a pris le relais
        self._refilling = False
        # La piste de départ a changé (ou la file a été vidée) : résultat périmé.
        if origin is not None and (self._current_ref is None
                                   or self._current_ref.video_id != origin):
            return GLib.SOURCE_REMOVE
        if refs:
            self.radio_reason = reason
        if not refs:
            if self._current_ref is not None:
                self._autoplay_failed_for = self._current_ref.video_id
                self._autoplay_retry_at = self._clock() + AUTOPLAY_RETRY
            return GLib.SOURCE_REMOVE
        was_idle = self.player.state == "idle"
        self._radio_ids.update(r.video_id for r in refs)
        self.enqueue(refs)
        if was_idle:
            self.queue.next()
        return GLib.SOURCE_REMOVE

    def drop_blocked_upcoming(self) -> int:
        """Retire de la suite les pistes proposées par la radio qui viennent
        d'être écartées. Ce que l'utilisateur a mis en file lui-même reste.
        Renvoie le nombre de pistes retirées."""
        blocked = self.library.blocked()
        queue = self.queue
        items = queue.items
        doomed = [i for i in queue.upcoming_indices()
                  if items[i].video_id in self._radio_ids
                  and is_blocked(items[i], blocked)]
        for index in sorted(doomed, reverse=True):
            queue.remove_at(index)
        return len(doomed)

    # ---- Historique ------------------------------------------------------

    def _note_skip(self) -> None:
        """Un « suivant » manuel avant 30 % de la piste est un saut : seul
        signal négatif. Jamais en session privée ; stop, précédent, saut
        direct, erreur et fin de piste n'en sont pas."""
        ref = self._current_ref
        if ref is None or self.settings.get("private_session"):
            return
        if self._playing_id != ref.video_id:     # jamais réellement lancée
            return
        played = self._max_position
        duration = self.player.duration or ref.duration
        early = (played / duration < SKIP_RATIO) if duration > 0 else played < 30.0
        if not early:
            return
        context = "radio" if ref.video_id in self._radio_ids else ""
        try:
            self.library.record_skip(ref, played, context)
        except Exception as exc:
            log.debug("Saut non enregistré : %s", exc)

    def _flush_history(self, completed: bool) -> None:
        """Enregistre l'écoute en cours si elle a duré assez longtemps."""
        ref = self._current_ref
        if ref is None or self._recorded or self.settings.get("private_session"):
            return
        played = self._max_position
        duration = self.player.duration or ref.duration
        ratio = float(self.settings.get("scrobble_after_ratio") or 0.5)
        long_enough = played >= 30.0 or (duration > 0 and played >= duration * ratio)
        if not long_enough:
            return
        self._recorded = True
        try:
            self.library.record_play(ref, played, completed)
        except Exception as exc:
            log.warning("Historique non enregistré: %s", exc)
        # Déclaration ListenBrainz, en arrière-plan et sans jamais bloquer.
        try:
            self.scrobbler.submit(ref, played, duration,
                                  started_at=self._started_at)
        except Exception as exc:
            log.debug("Scrobble impossible : %s", exc)

    # ---- État persistant -------------------------------------------------

    def save_state(self) -> None:
        snapshot = self.queue.snapshot()
        self.settings.update(
            last_queue=snapshot["items"],
            last_index=snapshot["index"],
            last_position=self._current_position() or self._restore_position,
            repeat=snapshot["repeat"],
            shuffle=snapshot["shuffle"],
        )
        self.settings.save()

    def wipe_user_data(self, delete_downloads: bool = False) -> None:
        """« Tout effacer » : bibliothèque, cookies, paroles, pochettes,
        file d'attente et tout ce qui trahit une écoute. Les téléphones
        appairés sont dissociés : leur prochaine synchro ne ressuscite rien."""
        self.cancel_sleep_timer()
        self.player.stop()
        self._current_ref = None
        self._playing_id = None
        self._reset_segments()
        self._load_token += 1               # une résolution en vol est jetée
        self._radio_token += 1              # une radio en vol est jetée
        self._refilling = False
        self._restore_position = 0.0
        self._failures = 0
        self._radio_ids.clear()
        self._autoplay_failed_for = None
        self.queue.set_items([])
        self.emit("track-changed", None)
        # D'abord couper la synchro et oublier les téléphones : une synchro
        # déjà en cours ne doit pas réécrire ce qu'on efface juste après.
        try:
            self.sync.cancel_pairing()
            self.sync.forget_all_devices()
            self.set_sync_enabled(False)
        except Exception as exc:
            log.debug("Dissociation des téléphones : %s", exc)
        # Le verrou attend la fin d'une fusion en vol ; `_sync` revérifie
        # ensuite que l'appareil est encore connu.
        with self.sync.drain():
            self.library.wipe()
            self.library.delete_corrupt_copies()
        # Plus aucun téléchargement en cours ; les fichiers seulement si demandé.
        try:
            self.downloads.cancel_all()
            if delete_downloads:
                self.downloads.delete_all_files()
        except Exception as exc:
            log.debug("Nettoyage des téléchargements : %s", exc)
        # Écoutes ListenBrainz en attente : elles trahissent l'historique.
        try:
            self.scrobbler.clear_queue()
        except Exception as exc:
            log.debug("File de scrobbles : %s", exc)
        try:
            from . import logs
            logs.truncate()
        except Exception as exc:
            log.debug("Journal : %s", exc)
        self.cookies.clear()
        try:
            from . import artwork
            artwork.clear_cache()
        except Exception as exc:
            log.debug("Cache des pochettes : %s", exc)
        self.settings.update(last_queue=[], last_index=0, last_position=0.0,
                             search_history=[])
        for key in ("visitor_data", "visitor_data_at"):
            self.settings.delete(key)
        reset_visitor = getattr(self.innertube, "reset_visitor", None)
        if reset_visitor is not None:
            reset_visitor()
        self.settings.save()
        self.emit("library-changed")
        self.emit("data-wiped")

    def backfill_durations_async(self, workers: int = 3) -> None:
        """Complète en arrière-plan les durées manquantes (imports oEmbed).

        Une seule passe à la fois ; une vidéo en échec n'est pas réessayée
        pendant la session.
        """
        if getattr(self, "_backfilling", False) or self.innertube is None:
            return
        failed = self.__dict__.setdefault("_duration_failures", set())
        todo = [v for v in self.library.missing_duration()
                if v not in failed and source_of(v) == "youtube"]
        if not todo:
            return
        self._backfilling = True
        lock = threading.Lock()
        done = [0]

        def worker() -> None:
            while True:
                with lock:
                    if not todo:
                        return
                    video_id = todo.pop()
                try:
                    seconds = self.innertube.video_length(video_id)
                except Exception:
                    seconds = None
                if seconds:
                    self.library.set_duration(video_id, seconds)
                    with lock:
                        done[0] += 1
                else:
                    failed.add(video_id)

        def run() -> None:
            threads = [threading.Thread(target=worker, daemon=True)
                       for _ in range(max(1, workers))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self._backfilling = False
            log.info("Durées complétées : %d", done[0])
            if done[0]:
                GLib.idle_add(lambda: (self.emit("library-changed"), False)[1])

        threading.Thread(target=run, daemon=True).start()

    def prune_cache_async(self) -> None:
        """Élague en arrière-plan le cache de la bibliothèque (pistes de
        recherche jamais gardées, segments, paroles, recommandations de plus
        de 90 jours). Une seule fois par lancement, sans jamais bloquer."""
        if getattr(self, "_pruned", False):
            return
        self._pruned = True

        def run() -> None:
            try:
                removed = self.library.prune_cache()
            except Exception as exc:
                log.debug("Élagage du cache impossible : %s", exc)
                return
            if any(removed.values()):
                log.info("Cache élagué : %s", removed)

        threading.Thread(target=run, daemon=True).start()

    def restore_state(self) -> bool:
        """Recharge la file précédente sans lancer la lecture.

        Renvoie True si quelque chose a été restauré.
        """
        items = self.settings.get("last_queue") or []
        if not items:
            return False
        index = self.settings.get("last_index") or 0
        if not self.queue.restore(
                {"items": items, "index": index if isinstance(index, int) else 0,
                 "shuffle": self.queue.shuffle, "repeat": self.queue.repeat},
                silent=True):
            return False
        self._current_ref = self.queue.current
        self._restore_position = float(self.settings.get("last_position") or 0.0)
        self.emit("queue-changed")
        self.emit("track-changed", self._current_ref)
        return True

    def resume(self) -> None:
        """Reprend la piste restaurée là où elle s'était arrêtée."""
        ref = self.queue.current
        if ref is None:
            return
        position = self._restore_position
        self._restore_position = 0.0
        self._on_queue_current(self.queue, ref)
        if position > RESTART_THRESHOLD:
            token = self._load_token
            attempts = [0]

            def seek_when_ready() -> bool:
                attempts[0] += 1
                if token != self._load_token or attempts[0] > 50:
                    return False          # autre piste, ou abandon après 15 s
                if self.player.state in ("playing", "paused"):
                    self.player.seek(position)
                    return False
                return True   # réessayer au prochain tick
            GLib.timeout_add(300, seek_when_ready)

    def shutdown(self) -> None:
        # D'abord le serveur : plus aucune synchro ne touche la bibliothèque
        # pendant qu'on la ferme.
        try:
            self.sync.stop()
        except Exception as exc:
            log.debug("Arrêt de la synchro: %s", exc)
        self._clear_sleep(restore=False)
        self._flush_history(completed=False)
        self.save_state()
        self.player.shutdown()
        if self._autosave_id:
            GLib.source_remove(self._autosave_id)
            self._autosave_id = 0
        # La fusion de synchro en vol se termine avant la fermeture.
        with self.sync.drain():
            self.library.close()
        # Le fichier de cookies en clair ne survit pas à la session.
        try:
            self.cookies.release()
        except Exception as exc:
            log.debug("Nettoyage des cookies: %s", exc)

    # ---- Divers ----------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        """Compte les requêtes en vol : True en ouvre une, False en ferme une.
        « busy » n'est émis qu'aux transitions 0 -> 1 et 1 -> 0."""
        if busy:
            self._busy_count += 1
        else:
            self._busy_count = max(0, self._busy_count - 1)
        now = self._busy_count > 0
        if now != self._busy:
            self._busy = now
            self.emit("busy", now)

    def _fail(self, message: str) -> bool:
        self._set_busy(False)
        first_line = message.strip().splitlines()[0] if message.strip() else message
        self.emit("error", first_line)
        return GLib.SOURCE_REMOVE
