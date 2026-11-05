{
  lib,
  python314,
  ffmpeg,
  makeWrapper,
}:
python314.pkgs.buildPythonApplication {
  pname = "dopplergeist";
  version = "0.1.2";

  pyproject = true;

  src = lib.cleanSource ./.;

  build-system = with python314.pkgs; [ setuptools ];

  dependencies = with python314.pkgs; [
    numpy
    scipy
    numba
    pandas
    soundfile
    pyproj
    rasterio
    matplotlib
    requests
    pyarrow
    shapely
  ];

  nativeBuildInputs = [
    makeWrapper
  ];

  postInstall = ''
    wrapProgram $out/bin/dopplergeist \
      --prefix PATH : ${
        lib.makeBinPath [
          ffmpeg
        ]
      }
  '';

  pythonImportsCheck = [
    "dopplergeist"
  ];

  meta = {
    description = "Aircraft acoustic geolocation using Doppler shifts";
    homepage = "https://github.com/1337isnot1337/DOPPLERGEIST";
    license = {
      spdxId = "PolyForm-Noncommercial-1.0.0";
      fullName = "PolyForm Noncommercial License 1.0.0";
      url = "https://polyformproject.org/licenses/noncommercial/1.0.0";
      free = false;
    };
    maintainers = with lib.maintainers; [ kleindotc ];
    mainProgram = "dopplergeist";
    platforms = lib.platforms.linux;
  };
}
