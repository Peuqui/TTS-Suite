// The texts of the page in German and English; the language follows the browser until it is switched.
const TEXTS = {
  de: {
    token: 'Token', language: 'English', themeLight: 'Hell', themeDark: 'Dunkel', themeAuto: 'Automatisch',
    tabCatalog: 'Engines', tabConfig: 'Konfiguration', tabVoices: 'Stimmen', tabSpeak: 'Probesprechen',
    reload: 'Neu laden', save: 'Speichern', saved: 'Gespeichert.', add: 'Hinzufügen', remove: 'Entfernen',
    up: 'Nach oben', down: 'Nach unten', start: 'Starten', stop: 'Stoppen', started: 'Gestartet.', stopped: 'Gestoppt.', busyStart: 'startet und lädt …', busyStop: 'stoppt …', busyRelease: 'gibt frei …',
    release: 'Freigeben', releaseAll: 'Alle freigeben', releaseStopped: 'freigegeben', releaseNot_running: 'lief nicht', releaseIn_use: 'in Benutzung, nicht angefasst', releaseUnreachable: 'Rechner nicht erreichbar', releaseFailed: 'fehlgeschlagen',
    needsToken: 'Dafür ist das Token nötig (oben eintragen).',
    tokenKept: 'Token gespeichert (in diesem Browser).', tokenCleared: 'Token entfernt.',
    catalogHint: 'Jede Engine auf jedem Rechner, auf dem sie laufen kann. Eine Anfrage wählt daraus ihre eigene Liste.',
    statusRunning: 'läuft', statusStarting: 'startet', statusStopped: 'aus', statusUnreachable: 'nicht erreichbar',
    stateRunning: 'läuft', stateStarting: 'startet', stateStopped: 'Rechner da, Container aus', stateUnreachable: 'Rechner nicht erreichbar (aus oder nicht im Netz)',
    engine: 'Engine', host: 'Rechner', thisMachine: 'dieser Rechner', status: 'Zustand', installed: 'Installiert',
    gpuMemory: 'Grafikspeicher (MiB)', hint: 'Hinweis', yes: 'ja', no: 'nein', cloud: 'Cloud', needsGpu: 'GPU',
    unit: 'Einheit', unitSentence: 'Satz', unitParagraph: 'Absatz', unitWhole: 'ganzer Text',
    voicesDir: 'Stimmenordner', hosts: 'Rechner (SSH)', hostName: 'Name', hostAddress: 'Adresse', sshUser: 'SSH-Benutzer', sshPort: 'SSH-Port',
    gpus: 'Grafikkarten', gpuModeAuto: 'Automatisch (beste Karte)', gpuModeList: 'Nur diese Karten (Liste)',
    gpuModeHint: 'Automatisch nimmt zur Laufzeit die Karte mit dem meisten freien Speicher, auf die die Engine passt. Die Liste probiert nur die genannten Karten der Reihe nach.', gpu: 'Karte (Nummer oder UUID)', reserve: 'Reserve (MiB)',
    headroom: 'Aufschlag je Engine bei der Kartenwahl (MiB)', engineDefaults: 'Vorgaben je Engine',
    notAFactor: 'Keine gültige Zahl über 0 (zum Beispiel 1,15).',
    tuningOf: 'Einstellungen der Stimme {voice}', tuningHint: 'Tempo und Tonhöhe, mit denen diese Stimme immer spricht. 1 = unverändert, sinnvoll sind etwa 0,5 bis 2. Die Tonhöhe ändert auch das Tempo: höher klingt schneller. Eine Anfrage kann beides überschreiben. Ein leeres Feld nimmt den grau angezeigten Wert.',
    reset: 'Zurücksetzen', close: 'Schließen',
    defaultVoice: 'Stimme', defaultSpeed: 'Tempo', defaultPitch: 'Tonhöhe',
    engineDefaultsHint: 'Gilt, wenn eine Anfrage nichts anderes angibt: Stimme, Tempo und Tonhöhe je Engine (1 = unverändert, sinnvoll etwa 0,5 bis 2; die Tonhöhe ändert auch das Tempo) und die Grenzen der Engine. Was eine Anfrage selbst mitschickt, geht vor. Ein leeres Feld nimmt den grau angezeigten Wert der Engine.',
    vramBudget: 'Speicherbudget (MiB)', maxParallel: 'Gleichzeitig', maxQueue: 'Warteschlange',
    pronunciations: 'Aussprache', word: 'Wort', spoken: 'gesprochen als', emptyMeansDefault: 'Vorgabe',
    voicesHint: 'Die Stimmen einer Engine, wie der Dienst sie kennt. Der Abgleich lädt die Stimmen des gemeinsamen Ordners zu einer Cloud-Engine hoch, die Stimmen klont.',
    push: 'An Hosts senden', pushed: 'Stimmen gesendet an: {hosts}.', pushedNone: 'Keine Hosts eingetragen.', sync: 'Abgleichen', synced: 'Abgeglichen: {count} Stimmen.', noVoices: 'Keine Stimmen.', voice: 'Stimme', load: 'Laden',
    speakHint: 'Nur für dieses Probesprechen: Hake die Engines an, die sprechen sollen, und ordne sie: sie sprechen nacheinander in dieser Reihenfolge, jede mit demselben Text (Vergleich). Die Suite führt keine eigene Liste, jedes Programm bringt seine mit (Agent-Orc und AIfred haben ihre eigene). Jede Zeile hat ihre eigene Stimme, auch dieselbe Engine auf zwei Rechnern. „Speichern“ merkt sich diese Auswahl auf dem Server für alle Geräte.',
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
    release: 'Release', releaseAll: 'Release all', releaseStopped: 'released', releaseNot_running: 'was not running', releaseIn_use: 'in use, left alone', releaseUnreachable: 'machine not reachable', releaseFailed: 'failed',
    needsToken: 'This needs the token (enter it above).',
    tokenKept: 'Token saved (in this browser).', tokenCleared: 'Token removed.',
    catalogHint: 'Every engine on every machine it can run on. A request chooses its own list from them.',
    statusRunning: 'running', statusStarting: 'starting', statusStopped: 'off', statusUnreachable: 'unreachable',
    stateRunning: 'running', stateStarting: 'starting', stateStopped: 'machine there, container off', stateUnreachable: 'machine not reachable (off or not on the network)',
    engine: 'Engine', host: 'Machine', thisMachine: 'this machine', status: 'Status', installed: 'Installed',
    gpuMemory: 'GPU memory (MiB)', hint: 'Hint', yes: 'yes', no: 'no', cloud: 'cloud', needsGpu: 'GPU',
    unit: 'Unit', unitSentence: 'sentence', unitParagraph: 'paragraph', unitWhole: 'whole text',
    voicesDir: 'Voices folder', hosts: 'Machines (SSH)', hostName: 'Name', hostAddress: 'Address', sshUser: 'SSH user', sshPort: 'SSH port',
    gpus: 'GPUs', gpuModeAuto: 'Automatic (best fit)', gpuModeList: 'Only these cards (list)',
    gpuModeHint: 'Automatic takes the card with the most free memory that the engine fits on, at run time. The list tries only the named cards, in order.', gpu: 'Card (index or UUID)', reserve: 'Reserve (MiB)',
    headroom: 'Margin per engine when a card is chosen (MiB)', engineDefaults: 'Defaults per engine',
    notAFactor: 'Not a valid number above 0 (for example 1.15).',
    tuningOf: 'Settings of the voice {voice}', tuningHint: 'Speed and pitch this voice always speaks with. 1 = unchanged, about 0.5 to 2 is sensible. Pitch changes the speed too: higher sounds faster. A request can override both. An empty field takes the value shown in grey.',
    reset: 'Reset', close: 'Close',
    defaultVoice: 'Voice', defaultSpeed: 'Speed', defaultPitch: 'Pitch',
    engineDefaultsHint: 'Used when a request says nothing else: voice, speed and pitch per engine (1 = unchanged, about 0.5 to 2 is sensible; pitch changes the speed too) and the limits of the engine. What a request sends itself wins. An empty field takes the engine\'s value shown in grey.',
    vramBudget: 'Memory budget (MiB)', maxParallel: 'In parallel', maxQueue: 'Queue',
    pronunciations: 'Pronunciation', word: 'Word', spoken: 'spoken as', emptyMeansDefault: 'default',
    voicesHint: 'The voices of an engine as the service knows them. Syncing uploads the voices of the shared folder to a cloud engine that clones voices.',
    push: 'Send to hosts', pushed: 'Voices sent to: {hosts}.', pushedNone: 'No hosts configured.', sync: 'Sync', synced: 'Synced: {count} voices.', noVoices: 'No voices.', voice: 'Voice', load: 'Load',
    speakHint: 'For this test reading only: tick the engines that should speak and order them: they speak one after the other in this order, each with the same text (a comparison). The suite keeps no list of its own, every program brings its own (Agent-Orc and AIfred have theirs). Every row has its own voice, also the same engine on two machines. Save keeps this choice on the server, for every device.',
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
