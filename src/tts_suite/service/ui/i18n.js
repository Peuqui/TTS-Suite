// The texts of the page in German and English; the language follows the browser until it is switched.
const TEXTS = {
  de: {
    token: 'Token', language: 'English', themeLight: 'Hell', themeDark: 'Dunkel', themeAuto: 'Automatisch',
    tabCatalog: 'Engines', tabConfig: 'Konfiguration', tabVoices: 'Stimmen', tabSpeak: 'Probesprechen',
    reload: 'Neu laden', save: 'Speichern', saved: 'Gespeichert.', add: 'Hinzufügen', remove: 'Entfernen',
    up: 'Nach oben', down: 'Nach unten', start: 'Starten', stop: 'Stoppen', started: 'Gestartet.', stopped: 'Gestoppt.', busyStart: 'startet und lädt …', busyStop: 'stoppt …', busyRelease: 'gibt frei …',
    release: 'Freigeben', releaseAll: 'Alle freigeben', releaseStopped: 'freigegeben', releaseReleased: 'freigegeben', releaseNot_running: 'lief nicht', releaseIn_use: 'in Benutzung, nicht angefasst', releaseBusy: 'in Benutzung oder startet, nicht angefasst', releaseUnreachable: 'Rechner nicht erreichbar', releaseFailed: 'fehlgeschlagen',
    needsToken: 'Dafür ist das Token nötig (oben eintragen).',
    tokenKept: 'Token gespeichert (in diesem Browser).', tokenCleared: 'Token entfernt.',
    catalogHint: 'Jede Engine auf jedem Rechner, auf dem sie laufen kann. Eine Anfrage wählt daraus ihre eigene Liste oder nimmt mit „auto“ die Automatik der Suite (Konfiguration).',
    statusRunning: 'bereit', statusStarting: 'startet', statusStopped: 'aus', statusUnreachable: 'nicht erreichbar',
    stateRunning: 'bereit – antwortet sofort auf Anfragen (Piper, eSpeak und Cloud-Engines ohne eigenen Prozess)', stateStarting: 'startet', stateStopped: 'Rechner da, Container aus', stateUnreachable: 'Rechner nicht erreichbar (aus oder nicht im Netz)',
    engine: 'Engine', host: 'Rechner', thisMachine: 'dieser Rechner', status: 'Zustand', installed: 'Installiert',
    gpuMemory: 'Grafikspeicher (MiB)', memoryNeeded: 'Bedarf auf der Karte (MiB)', hint: 'Hinweis', yes: 'ja', no: 'nein', cloud: 'Cloud', needsGpu: 'GPU',
    unit: 'Einheit', unitSentence: 'Satz', unitParagraph: 'Absatz', unitWhole: 'ganzer Text',
    voicesDir: 'Stimmenordner', hosts: 'Rechner (SSH)', hostName: 'Name', hostAddress: 'Adresse', sshUser: 'SSH-Benutzer', sshPort: 'SSH-Port', hostGpuOrc: 'GPU-Orc dort (Adresse)', hostGpuOrcToken: 'Token-Datei dieses GPU-Orc',
    gpus: 'Grafikkarten', gpuModeAuto: 'Automatisch (beste Karte)', gpuModeList: 'Nur diese Karten (Liste)',
    gpuModeHint: 'Automatisch nimmt zur Laufzeit die Karte mit dem meisten freien Speicher, auf die die Engine passt. Die Liste probiert nur die genannten Karten der Reihe nach.', gpu: 'Karte (Nummer oder UUID)', reserve: 'Reserve (MiB)',
    headroom: 'Aufschlag je Engine bei der Kartenwahl (MiB)', engineDefaults: 'Vorgaben je Engine',
    groupAuto: 'Automatik (engines: "auto")', groupAutoHint: 'Die Reihenfolge, die eine Anfrage mit „auto“ von oben nach unten durchgeht: der erste Eintrag, der gerade sprechen kann, spricht; fällt er aus, übernimmt der nächste. Eine GPU-Engine braucht den freien Grafikspeicher rechts, eine CPU-Engine nimmt immer, eine Cloud-Engine braucht ihren Zugang. Einträge lassen sich ausschalten, verschieben und in der Einheit ändern; eine neue Engine kommt ausgeschaltet ans Ende. Ein Programm, das seine eigene Liste mitschickt, nutzt diese Liste nicht.',
    needsFree: '{mib} MiB frei', cpuAlways: 'CPU, nimmt immer', notOffered: 'nicht mehr im Katalog',
    notAFactor: 'Keine gültige Zahl über 0 (zum Beispiel 1,15).',
    tuningOf: 'Einstellungen der Stimme {voice}', tuningHint: 'Tempo und Tonhöhe, mit denen diese Stimme immer spricht. 1 = unverändert, sinnvoll sind etwa 0,5 bis 2. Die Tonhöhe ändert auch das Tempo: höher klingt schneller. Eine Anfrage kann beides überschreiben. Ein leeres Feld nimmt den grau angezeigten Wert.',
    reset: 'Zurücksetzen', close: 'Schließen',
    defaultVoice: 'Stimme', defaultSpeed: 'Tempo', defaultPitch: 'Tonhöhe',
    engineDefaultsHint: 'Gilt, wenn eine Anfrage nichts anderes angibt: Stimme, Tempo und Tonhöhe je Engine (1 = unverändert, sinnvoll etwa 0,5 bis 2; die Tonhöhe ändert auch das Tempo) und die Grenzen der Engine. Was eine Anfrage selbst mitschickt, geht vor. Ein leeres Feld nimmt den grau angezeigten Wert der Engine.',
    burnIn: 'Burn-in', burnInConfirm: 'Burn-in für {engine}: Die Engine wird ohne Speichergrenze auf einer unbenutzten Karte neu gestartet, liest zwei lange Texte (einige Minuten) und wird danach gestoppt. Die gemessene Spitze wird ihr Speicherbudget.',
    burnInStarting: 'Burn-in {engine}: startet …', burnInRunning: 'Burn-in {engine}: Text {round} von {rounds}, Spitze bisher {peak} MiB', burnInDone: 'Burn-in {engine} fertig: {peak} MiB als Speicherbudget eingetragen.',
    vramBudget: 'Speicherbudget (MiB)', maxParallel: 'Gleichzeitig', maxQueue: 'Warteschlange',
    pronunciations: 'Aussprache', word: 'Wort', spoken: 'gesprochen als', emptyMeansDefault: 'Vorgabe',
    voicesHint: 'Die Stimmen einer Engine, wie der Dienst sie kennt. Der Abgleich lädt die Stimmen des gemeinsamen Ordners zu einer Cloud-Engine hoch, die Stimmen klont.',
    push: 'An Hosts senden', pushed: 'Stimmen gesendet an: {hosts}.', pushedNone: 'Keine Hosts eingetragen.', sync: 'Abgleichen', synced: 'Abgeglichen: {count} Stimmen.', noVoices: 'Keine Stimmen.', voice: 'Stimme', load: 'Laden',
    speakHint: 'Nur für dieses Probesprechen: Hake die Engines an, die sprechen sollen, und ordne sie: „Sprechen“ lässt sie nacheinander in dieser Reihenfolge sprechen, jede mit demselben Text (Vergleich). „Mit Automatik“ liest einmal so, wie ein Programm mit „auto“ es bekommt: nach der Liste der Automatik (Konfiguration), der Verlauf zeigt, wer übergangen wurde, wer ausfiel und wer sprach. Jede Zeile hat ihre eigene Stimme, auch dieselbe Engine auf zwei Rechnern; die Automatik nimmt dieselben Stimmen. „Speichern“ merkt sich diese Auswahl auf dem Server für alle Geräte.',
    speakAuto: 'Mit Automatik', speakAutoHint: 'Eine Lesung nach der Liste der Automatik (Konfiguration)',
    text: 'Text', pause: 'Pause zwischen Absätzen (ms)', language2: 'Sprache', use: 'Verwenden', order: 'Reihenfolge', speak: 'Sprechen', speaking: 'Spricht …',
    stopPlaying: 'Abbrechen', trace: 'Verlauf', entry: 'Eintrag', outcome: 'Ergebnis', reason: 'Grund', detail: 'Einzelheiten',
    outcomeSpoke: 'gesprochen', outcomeFailed: 'ausgefallen', outcomeSkipped: 'übergangen', switched: 'Stimmwechsel angesagt: {reason}',
    nothingChosen: 'Wähle mindestens einen Eintrag.', failed: 'Fehlgeschlagen',
  },
  en: {
    token: 'Token', language: 'Deutsch', themeLight: 'Light', themeDark: 'Dark', themeAuto: 'Automatic',
    tabCatalog: 'Engines', tabConfig: 'Configuration', tabVoices: 'Voices', tabSpeak: 'Try speaking',
    reload: 'Reload', save: 'Save', saved: 'Saved.', add: 'Add', remove: 'Remove',
    up: 'Up', down: 'Down', start: 'Start', stop: 'Stop', started: 'Started.', stopped: 'Stopped.', busyStart: 'starting and loading …', busyStop: 'stopping …', busyRelease: 'releasing …',
    release: 'Release', releaseAll: 'Release all', releaseStopped: 'released', releaseReleased: 'released', releaseNot_running: 'was not running', releaseIn_use: 'in use, left alone', releaseBusy: 'in use or starting, left alone', releaseUnreachable: 'machine not reachable', releaseFailed: 'failed',
    needsToken: 'This needs the token (enter it above).',
    tokenKept: 'Token saved (in this browser).', tokenCleared: 'Token removed.',
    catalogHint: 'Every engine on every machine it can run on. A request chooses its own list from them, or takes the suite\'s automatic list with "auto" (Configuration).',
    statusRunning: 'ready', statusStarting: 'starting', statusStopped: 'off', statusUnreachable: 'unreachable',
    stateRunning: 'ready – answers requests at once (Piper, eSpeak and cloud engines have no process of their own)', stateStarting: 'starting', stateStopped: 'machine there, container off', stateUnreachable: 'machine not reachable (off or not on the network)',
    engine: 'Engine', host: 'Machine', thisMachine: 'this machine', status: 'Status', installed: 'Installed',
    gpuMemory: 'GPU memory (MiB)', memoryNeeded: 'Needed on a card (MiB)', hint: 'Hint', yes: 'yes', no: 'no', cloud: 'cloud', needsGpu: 'GPU',
    unit: 'Unit', unitSentence: 'sentence', unitParagraph: 'paragraph', unitWhole: 'whole text',
    voicesDir: 'Voices folder', hosts: 'Machines (SSH)', hostName: 'Name', hostAddress: 'Address', sshUser: 'SSH user', sshPort: 'SSH port', hostGpuOrc: 'GPU-Orc there (address)', hostGpuOrcToken: 'Token file of that GPU-Orc',
    gpus: 'GPUs', gpuModeAuto: 'Automatic (best fit)', gpuModeList: 'Only these cards (list)',
    gpuModeHint: 'Automatic takes the card with the most free memory that the engine fits on, at run time. The list tries only the named cards, in order.', gpu: 'Card (index or UUID)', reserve: 'Reserve (MiB)',
    headroom: 'Margin per engine when a card is chosen (MiB)', engineDefaults: 'Defaults per engine',
    groupAuto: 'Automatic (engines: "auto")', groupAutoHint: 'The order a request with "auto" goes through from the top: the first entry that can speak now speaks; if it fails, the next one takes over. A GPU engine needs the free graphics memory on the right, a CPU engine always takes it, a cloud engine needs its access. Entries can be switched off, moved and given another unit; a new engine joins at the end, switched off. A program that brings its own list does not use this one.',
    needsFree: '{mib} MiB free', cpuAlways: 'CPU, always takes it', notOffered: 'no longer in the catalog',
    notAFactor: 'Not a valid number above 0 (for example 1.15).',
    tuningOf: 'Settings of the voice {voice}', tuningHint: 'Speed and pitch this voice always speaks with. 1 = unchanged, about 0.5 to 2 is sensible. Pitch changes the speed too: higher sounds faster. A request can override both. An empty field takes the value shown in grey.',
    reset: 'Reset', close: 'Close',
    defaultVoice: 'Voice', defaultSpeed: 'Speed', defaultPitch: 'Pitch',
    engineDefaultsHint: 'Used when a request says nothing else: voice, speed and pitch per engine (1 = unchanged, about 0.5 to 2 is sensible; pitch changes the speed too) and the limits of the engine. What a request sends itself wins. An empty field takes the engine\'s value shown in grey.',
    burnIn: 'Burn-in', burnInConfirm: 'Burn-in of {engine}: the engine is restarted without a memory limit on an unused card, reads two long texts (a few minutes) and is stopped afterwards. The measured peak becomes its memory budget.',
    burnInStarting: 'Burn-in {engine}: starting …', burnInRunning: 'Burn-in {engine}: text {round} of {rounds}, peak so far {peak} MiB', burnInDone: 'Burn-in {engine} done: {peak} MiB entered as the memory budget.',
    vramBudget: 'Memory budget (MiB)', maxParallel: 'In parallel', maxQueue: 'Queue',
    pronunciations: 'Pronunciation', word: 'Word', spoken: 'spoken as', emptyMeansDefault: 'default',
    voicesHint: 'The voices of an engine as the service knows them. Syncing uploads the voices of the shared folder to a cloud engine that clones voices.',
    push: 'Send to hosts', pushed: 'Voices sent to: {hosts}.', pushedNone: 'No hosts configured.', sync: 'Sync', synced: 'Synced: {count} voices.', noVoices: 'No voices.', voice: 'Voice', load: 'Load',
    speakHint: 'For this test reading only: tick the engines that should speak and order them: Speak lets them speak one after the other in this order, each with the same text (a comparison). With Automatic reads once the way a program that says "auto" gets it: by the automatic list (Configuration); the course shows who was passed over, who failed and who spoke. Every row has its own voice, also the same engine on two machines; Automatic takes the same voices. Save keeps this choice on the server, for every device.',
    speakAuto: 'With Automatic', speakAutoHint: 'One reading by the automatic list (Configuration)',
    text: 'Text', pause: 'Pause between paragraphs (ms)', language2: 'Language', use: 'Use', order: 'Order', speak: 'Speak', speaking: 'Speaking …',
    stopPlaying: 'Cancel', trace: 'Course', entry: 'Entry', outcome: 'Outcome', reason: 'Reason', detail: 'Details',
    outcomeSpoke: 'spoke', outcomeFailed: 'failed', outcomeSkipped: 'skipped', switched: 'Voice change announced: {reason}',
    nothingChosen: 'Choose at least one entry.', failed: 'Failed',
  },
}

const STORAGE_KEY = 'tts-suite-language'
let language = localStorage.getItem(STORAGE_KEY) ?? (navigator.language.startsWith('de') ? 'de' : 'en')
const listeners = []

export function currentLanguage() { return language }
export function onLanguageChange(listener) { listeners.push(listener) }
export function toggleLanguage() {
  language = language === 'de' ? 'en' : 'de'
  localStorage.setItem(STORAGE_KEY, language)
  document.documentElement.lang = language
  listeners.forEach((listener) => listener())
}
export function t(key, values = {}) {
  const text = TEXTS[language][key] ?? TEXTS.en[key] ?? key
  return text.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ''))
}

// Where an engine of the catalog runs, as the pages show it: a cloud engine in the cloud, else the
// machine named, else this one.
export function hostLabel(row) {
  return row.cloud ? t('cloud') : (row.host ?? t('thisMachine'))
}
