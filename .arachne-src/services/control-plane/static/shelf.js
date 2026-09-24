const root = document.querySelector("[data-shelf-root]");
if (root) {
  const token = root.dataset.token;
  const statusNode = root.querySelector("[data-shelf-status]");
  const form = root.querySelector("[data-shelf-form]");
  const upcInput = root.querySelector("[data-shelf-upc]");
  const video = root.querySelector("[data-shelf-video]");
  const canvas = root.querySelector("[data-shelf-canvas]");
  const startCamera = root.querySelector("[data-shelf-start-camera]");
  const captureBarcode = root.querySelector("[data-shelf-capture-barcode]");
  const barcodePhoto = root.querySelector("[data-shelf-barcode-photo]");
  const photoInput = root.querySelector("[data-shelf-photo]");
  const submitButton = root.querySelector("[data-shelf-submit]");
  const resultNode = root.querySelector("[data-shelf-result]");
  const errorNode = root.querySelector("[data-shelf-error]");
  let stream;
  let scanControls;

  function showError(message) {
    errorNode.hidden = false;
    errorNode.textContent = message;
  }

  function clearMessages() {
    errorNode.hidden = true;
    errorNode.textContent = "";
    resultNode.hidden = true;
    resultNode.textContent = "";
  }

  function reportDiagnostic(event, detail = {}) {
    fetch(`/api/v1/shelf/${encodeURIComponent(token)}/diagnostics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, detail }),
      keepalive: true,
    }).catch(() => {});
  }

  async function refreshStatus() {
    try {
      const response = await fetch(`/api/v1/shelf/${encodeURIComponent(token)}`);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || "Unable to reach this sign.");
      }
      const battery = payload.battery_telemetry;
      const batteryText = battery?.level_percent != null
        ? ` · Battery ${battery.level_percent}%`
        : "";
      statusNode.innerHTML = payload.online
        ? `<span class="status active">Sign is online${batteryText}</span>`
        : `<span class="status draft">Sign is waking up — wait a few seconds and try again</span>`;
    } catch (error) {
      statusNode.innerHTML = `<span class="status cancelled">${error.message}</span>`;
    }
  }

  async function startScanner() {
    if (!navigator.mediaDevices?.getUserMedia) {
      showError("Camera access is not available in this browser.");
      return;
    }
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
      },
      audio: false,
    });
    video.srcObject = stream;
    await video.play();
    const track = stream.getVideoTracks()[0];
    const capabilities = track.getCapabilities?.() || {};
    const advanced = [];
    if (capabilities.focusMode?.includes?.("continuous")) {
      advanced.push({ focusMode: "continuous" });
    }
    if (capabilities.zoom && capabilities.zoom.max >= 1.5) {
      advanced.push({ zoom: Math.min(2, capabilities.zoom.max) });
    }
    if (advanced.length) {
      await track.applyConstraints({ advanced }).catch(() => {});
    }
    reportDiagnostic("camera-started", {
      width: video.videoWidth,
      height: video.videoHeight,
      capabilities,
    });
    captureBarcode.hidden = false;
  }

  async function detectWithNativeApi() {
    const detector = new BarcodeDetector({
      formats: ["ean_13", "ean_8", "upc_a", "upc_e"],
    });
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      const codes = await detector.detect(canvas);
      if (codes.length) return codes[0].rawValue;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return "";
  }

  async function detectWithZxing() {
    const reader = new ZXingBrowser.BrowserMultiFormatReader();
    return new Promise(async (resolve, reject) => {
      const timeout = setTimeout(() => {
        if (scanControls) scanControls.stop();
        resolve("");
      }, 30000);
      try {
        scanControls = await reader.decodeFromVideoElement(
          video,
          (result, _error, controls) => {
            if (!result) return;
            clearTimeout(timeout);
            controls.stop();
            scanControls = undefined;
            resolve(result.getText());
          },
        );
      } catch (error) {
        clearTimeout(timeout);
        reject(error);
      }
    });
  }

  function acceptBarcode(rawValue) {
    let upc = String(rawValue || "").replace(/\D/g, "");
    if (upc.length === 13 && upc.startsWith("0")) upc = upc.slice(1);
    if (!upc) return false;
    upcInput.value = upc;
    clearMessages();
    resultNode.hidden = false;
    resultNode.textContent = `Barcode captured: ${upc}`;
    return true;
  }

  async function captureFrameBlob() {
    const track = stream?.getVideoTracks?.()[0];
    if (track && typeof ImageCapture === "function") {
      try {
        const photo = await new ImageCapture(track).takePhoto();
        if (photo?.size) {
          reportDiagnostic("live-frame-photo", { size: photo.size, type: photo.type });
          return photo;
        }
      } catch (error) {
        reportDiagnostic("live-frame-photo-failed", {
          name: error?.name,
          message: error?.message,
        });
      }
    }
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const sample = context.getImageData(
      Math.max(0, Math.floor(canvas.width / 2) - 4),
      Math.max(0, Math.floor(canvas.height / 2) - 4),
      8,
      8,
    ).data;
    let brightest = 0;
    for (let index = 0; index < sample.length; index += 4) {
      brightest = Math.max(brightest, sample[index], sample[index + 1], sample[index + 2]);
    }
    if (brightest < 8) {
      reportDiagnostic("live-frame-blank", { width: canvas.width, height: canvas.height });
      return null;
    }
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
    reportDiagnostic("live-frame-canvas", {
      width: canvas.width,
      height: canvas.height,
      size: blob?.size || 0,
    });
    return blob;
  }

  async function decodeFrameOnServer(blob) {
    const upload = new FormData();
    upload.set("image", blob, "barcode.jpg");
    const response = await fetch(
      `/api/v1/shelf/${encodeURIComponent(token)}/decode-barcode`,
      { method: "POST", body: upload },
    );
    const payload = await response.json().catch(() => ({}));
    if (response.ok && acceptBarcode(payload.upc)) {
      reportDiagnostic("live-frame-decoded", { upc: payload.upc, size: blob.size });
      return true;
    }
    reportDiagnostic("live-frame-miss", {
      status: response.status,
      detail: payload.detail,
      size: blob.size,
    });
    return false;
  }

  async function captureBarcodeFromVideo() {
    if (!video.videoWidth || !video.videoHeight) {
      showError("The camera is still starting. Wait a moment and try again.");
      reportDiagnostic("live-camera-not-ready", {
        width: video.videoWidth,
        height: video.videoHeight,
      });
      return;
    }
    captureBarcode.disabled = true;
    captureBarcode.textContent = "Scanning…";
    resultNode.hidden = false;
    resultNode.textContent = "Reading the barcode…";
    try {
      const track = stream?.getVideoTracks?.()[0];
      if (track) {
        await track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
      }
      for (let attempt = 0; attempt < 3; attempt += 1) {
        if (attempt) await new Promise((resolve) => setTimeout(resolve, 400));
        const blob = await captureFrameBlob();
        if (blob && await decodeFrameOnServer(blob)) return;
      }
      reportDiagnostic("live-scan-timeout");
      resultNode.hidden = true;
      showError("No barcode detected. Fill the frame, hold steady, and try again.");
    } catch (error) {
      reportDiagnostic("live-scan-error", {
        name: error?.name,
        message: error?.message,
      });
      resultNode.hidden = true;
      showError("Barcode scan failed. Hold steady and try again.");
    } finally {
      captureBarcode.disabled = false;
      captureBarcode.textContent = "Capture barcode";
    }
  }

  async function captureBarcodeFromPhoto(file) {
    if (!file) return;
    clearMessages();
    resultNode.hidden = false;
    resultNode.textContent = "Photo received — decoding barcode…";
    reportDiagnostic("photo-selected", {
      type: file.type,
      size: file.size,
      createImageBitmap: "createImageBitmap" in window,
    });
    try {
      const upload = new FormData();
      upload.set("image", file);
      const serverResponse = await fetch(
        `/api/v1/shelf/${encodeURIComponent(token)}/decode-barcode`,
        { method: "POST", body: upload },
      );
      const serverPayload = await serverResponse.json();
      if (serverResponse.ok && acceptBarcode(serverPayload.upc)) {
        reportDiagnostic("photo-server-success", { upc: serverPayload.upc });
        return;
      }
      reportDiagnostic("photo-server-miss", {
        status: serverResponse.status,
        detail: serverPayload.detail,
      });

      let source;
      let releaseSource = () => {};
      if ("createImageBitmap" in window) {
        source = await createImageBitmap(file, { imageOrientation: "from-image" });
        releaseSource = () => source.close();
      } else {
        const objectUrl = URL.createObjectURL(file);
        source = await new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = reject;
          image.src = objectUrl;
        });
        releaseSource = () => URL.revokeObjectURL(objectUrl);
      }
      const sourceWidth = source.width || source.naturalWidth;
      const sourceHeight = source.height || source.naturalHeight;
      reportDiagnostic("photo-loaded", {
        width: sourceWidth,
        height: sourceHeight,
      });
      const scale = Math.min(1, 2560 / Math.max(sourceWidth, sourceHeight));
      canvas.width = Math.round(sourceWidth * scale);
      canvas.height = Math.round(sourceHeight * scale);
      canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
      releaseSource();

      let rawValue = "";
      if ("BarcodeDetector" in window) {
        const detector = new BarcodeDetector({
          formats: ["ean_13", "ean_8", "upc_a", "upc_e"],
        });
        const codes = await detector.detect(canvas);
        if (codes.length) rawValue = codes[0].rawValue;
      } else if (window.ZXingBrowser) {
        const reader = new ZXingBrowser.BrowserMultiFormatReader();
        rawValue = reader.decodeFromCanvas(canvas).getText();
      }
      if (!acceptBarcode(rawValue)) {
        reportDiagnostic("photo-scan-empty");
        showError("No barcode found in the photo. Move closer and keep the label sharp.");
      } else {
        reportDiagnostic("photo-scan-success", { rawValue });
      }
    } catch (error) {
      reportDiagnostic("photo-scan-error", {
        name: error?.name,
        message: error?.message,
      });
      showError("The barcode photo could not be decoded. Try again closer to the label.");
    } finally {
      barcodePhoto.value = "";
    }
  }

  let ackTimer;

  async function pollDeploymentAck(deploymentId, deadline) {
    if (Date.now() > deadline) {
      resultNode.textContent +=
        " The sign has not confirmed yet; it will refresh on its next wake.";
      return;
    }
    try {
      const response = await fetch(
        `/api/v1/shelf/${encodeURIComponent(token)}/deployments/${encodeURIComponent(deploymentId)}`,
      );
      const payload = await response.json();
      if (response.ok) {
        resultNode.hidden = false;
        resultNode.textContent = payload.message || `Deployment ${payload.stage}.`;
        reportDiagnostic("deploy-ack", { stage: payload.stage });
        if (payload.stage === "displayed") return;
      }
    } catch (_error) {
      // Keep polling; transient network errors are expected on store WiFi.
    }
    ackTimer = setTimeout(
      () => pollDeploymentAck(deploymentId, deadline),
      5000,
    );
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessages();
    if (ackTimer) clearTimeout(ackTimer);
    submitButton.disabled = true;
    submitButton.textContent = "Sending…";
    try {
      const body = new FormData();
      body.set("upc", upcInput.value.trim());
      if (photoInput.files?.[0]) {
        body.set("compliance_photo", photoInput.files[0]);
      }
      const response = await fetch(`/api/v1/shelf/${encodeURIComponent(token)}/deploy`, {
        method: "POST",
        body,
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || "Campaign delivery failed.");
      }
      resultNode.hidden = false;
      resultNode.textContent =
        `Campaign sent to the sign for UPC ${payload.upc}. Waiting for the sign to confirm…`;
      form.reset();
      if (payload.deployment_id) {
        pollDeploymentAck(payload.deployment_id, Date.now() + 3 * 60 * 1000);
      }
    } catch (error) {
      showError(error.message);
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Send campaign to sign";
    }
  });

  startCamera.addEventListener("click", () => {
    clearMessages();
    startScanner().catch(() => showError("Camera permission was denied."));
  });
  captureBarcode.addEventListener("click", () => {
    clearMessages();
    captureBarcodeFromVideo().catch(() => showError("Barcode scan failed."));
  });
  barcodePhoto.addEventListener("change", () => {
    captureBarcodeFromPhoto(barcodePhoto.files?.[0]);
  });

  refreshStatus();
  reportDiagnostic("scanner-loaded", {
    userAgent: navigator.userAgent,
    native: "BarcodeDetector" in window,
    zxing: !!window.ZXingBrowser,
    mediaDevices: !!navigator.mediaDevices?.getUserMedia,
    createImageBitmap: "createImageBitmap" in window,
  });
  window.addEventListener("error", (event) => {
    reportDiagnostic("window-error", {
      message: event.message,
      filename: event.filename,
      line: event.lineno,
      column: event.colno,
    });
    showError(`Scanner error: ${event.message || "unexpected browser error"}`);
  });
  window.addEventListener("beforeunload", () => {
    if (scanControls) scanControls.stop();
    if (stream) stream.getTracks().forEach((track) => track.stop());
  });
}
