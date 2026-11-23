"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld(
  "forecontractDesktop",
  Object.freeze({
    getLocale: () => ipcRenderer.invoke("forecontract:get-locale"),
    setLocale: (locale) => ipcRenderer.invoke("forecontract:set-locale", locale),
  })
);
