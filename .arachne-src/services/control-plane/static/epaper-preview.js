(() => {
  const palette = [
    [0, 0, 0],
    [255, 255, 255],
    [255, 255, 0],
    [255, 0, 0],
    [0, 0, 255],
    [0, 255, 0],
  ];
  const panelWidth = 600;
  const panelHeight = 800;
  const dialog = document.querySelector("[data-epaper-modal]");
  const canvas = document.querySelector("[data-epaper-canvas]");
  const title = document.querySelector("[data-epaper-title]");
  const status = document.querySelector("[data-epaper-status]");
  if (!dialog || !canvas || !title || !status) return;

  const nearest = (red, green, blue) => {
    let best = palette[1];
    let bestDistance = Infinity;
    for (const color of palette) {
      const distance =
        (red - color[0]) ** 2 + (green - color[1]) ** 2 + (blue - color[2]) ** 2;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = color;
      }
    }
    return best;
  };

  const paint = (image) => {
    const context = canvas.getContext("2d", { willReadFrequently: true });
    canvas.width = panelWidth;
    canvas.height = panelHeight;
    const scale = Math.max(panelWidth / image.naturalWidth, panelHeight / image.naturalHeight);
    const drawWidth = image.naturalWidth * scale;
    const drawHeight = image.naturalHeight * scale;
    context.drawImage(
      image,
      (panelWidth - drawWidth) / 2,
      (panelHeight - drawHeight) / 2,
      drawWidth,
      drawHeight,
    );
    const frame = context.getImageData(0, 0, panelWidth, panelHeight);
    const pixels = frame.data;
    for (let index = 0; index < pixels.length; index += 4) {
      const [red, green, blue] = nearest(pixels[index], pixels[index + 1], pixels[index + 2]);
      pixels[index] = red;
      pixels[index + 1] = green;
      pixels[index + 2] = blue;
      pixels[index + 3] = 255;
    }
    context.putImageData(frame, 0, 0);
  };

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  document.querySelectorAll("[data-epaper-preview]").forEach((button) => {
    button.addEventListener("click", () => {
      const source = button.dataset.src;
      const name = button.dataset.name || "Sign preview";
      title.textContent = name;
      status.hidden = false;
      status.textContent = "Preparing the panel preview…";
      canvas.hidden = true;
      dialog.showModal();
      const image = new Image();
      image.onload = () => {
        paint(image);
        canvas.hidden = false;
        status.hidden = true;
      };
      image.onerror = () => {
        status.hidden = false;
        status.textContent = "The artwork could not be loaded for preview.";
      };
      image.src = source;
    });
  });
})();
