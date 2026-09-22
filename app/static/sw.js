// Absichtlich ohne Cache.
//
// Diese App ist offline nutzlos: jede Aktion ist ein POST, der ein Etikett
// druckt oder den Bestand ändert. Ein Cache-first-Worker würde bei
// Verbindungsproblemen veraltete Seiten ausliefern — ein Bestand, der nicht
// mehr stimmt, ist schlimmer als eine Fehlermeldung. Der Worker existiert
// nur, damit die App installierbar ist, und reicht alles durch.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  // Kein respondWith: der Browser holt die Anfrage normal aus dem Netz.
});
