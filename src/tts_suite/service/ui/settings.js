// The one place for the numbers and defaults of this page (as constants.py is for the service).

// How long a message stays before it fades (an error longer), in ms.
export const NOTICE_MS = { ok: 4000, error: 10000 }
// How often the catalog asks again while it is shown, so states and idle countdowns stay current (ms).
export const REFRESH_MS = 5000
// How often the page asks for the progress of a running burn-in (ms).
export const BURN_IN_POLL_MS = 1500
// Who asks, in the log of the service and in what a forced release tells a running reading.
export const RELEASE_REASON = 'TTS-Suite page'

// The test reading until something is saved: its text, its language, the languages offered, and the pause
// between paragraphs (the service requires one; this is a usual one) with the step of its field (ms).
export const TEST_TEXT = 'Hallo Welt. Das ist ein Test der Sprachausgabe.'
export const TEST_LANGUAGE = 'de'
export const TEST_LANGUAGES = ['de', 'en']
export const TEST_PAUSE_MS = 700
export const TEST_PAUSE_STEP_MS = 50
