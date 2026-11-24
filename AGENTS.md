# AGENTS.md — Directrices de Desarrollo para Protocol Emulator ASIC

## 1. Identidad y Alcance del Proyecto
- **Proyecto:** Protocol Emulator ASIC: micro-núcleo reprogramable de ciclo exacto (inspirado en RP2040 PIO / TI PRU) que implementa protocolos digitales por firmware, **no** bloques UART/SPI/I2C fijos.
- **Concurso:** Jane Street *Protocol Emulator ASIC Competition*. **Entrega: lunes 18-ene-2027.** Repo público con RTL, tests y GDS + precheck en verde, más docs de arquitectura/ISA, ensamblador con programas de ejemplo, write-up de verificación y uso declarado de IA. Criterio nº1: **flexibilidad** (soportar protocolos no previstos tras fabricar); verificación y write-up pesan mucho.
- **Silicio:** Tiny Tapeout sobre IHP 130 nm **CMOS5L** (`ihp-sg13cmos5l`), shuttle de marzo de 2027. Área máx. **8x4 tiles** (~1 mm², ~1K celdas/tile). Utilización objetivo < 0.65.
- **E/S:** `ui_in[7:0]`, `uo_out[7:0]`, `uio_in/uio_out/uio_oe[7:0]`, más `clk`, `rst_n` y `ena`. El reloj lo genera la RP2350 de la demo board (objetivo ≤ 50 MHz; 60 MHz confirmado como máximo). Pads con ~10 ns de ida y vuelta. Pull-ups y transceptores van en un Pmod externo.
- **Energía:** Al deseleccionar el diseño se pierde **todo** el estado, así que el host debe recargar la IMEM. Los flops no arrancan a 0: todo sale de `rst_n`.

## 2. Modos de Operación (Regla Prioritaria)
- **Modo Interactivo (POR DEFECTO — obligatorio salvo orden contraria):**
  1. **Plan antes de actuar:** Antes de crear/editar archivos o ejecutar comandos que modifiquen el repositorio, explica brevemente qué propones hacer, qué archivos se verán afectados y **espera confirmación explícita del usuario**.
  2. **Avance incremental:** Trabaja en pasos pequeños y verificables. No resuelvas varios bloques a la vez ni generes volcados masivos de código sin desglosar.
  3. **Alcance estricto:** Modifica únicamente lo pedido. No refactorices código no relacionado ni añadas instrumentación o funcionalidades extra por iniciativa propia.
- **Modo `[SOLO LECTURA]`:** Si el usuario inicia con `[SOLO LECTURA]` o pide analizar/explicar código, no edites archivos ni propongas modificaciones salvo que se solicite después.
- **Modo `[AUTÓNOMO]` (SOLO BAJO PETICIÓN EXPLÍCITA):**
  - Se activa únicamente cuando el usuario use `[AUTÓNOMO]` o una orden directa (ej. *"hazlo tú"*, *"impleméntalo directamente"*), y **solo** para tareas de Python (`model/`, tests cocotb, scripts) o documentación; **nunca** sustituye la regla de que el usuario escribe el Verilog de `src/`.
  - Incluso en modo autónomo: mantener `src/config.json`, `.github/workflows/` y `.devcontainer/` como solo lectura, no tocar Git sin permiso, no lanzar comandos lentos sin avisar y validar con `pytest` / lint / cocotb antes de cerrar.

## 3. Ejecución, Resumen y Control de Documentación
- **Comandos lentos prohibidos sin permiso:** Nunca ejecutes flujos pesados en local (como LibreLane/Docker en `.devcontainer`, campañas largas de `sby` o pushes que disparen `gds.yaml`) sin confirmación previa del usuario. Prioriza pruebas rápidas (`pytest -q model/`, `verilator --lint-only`, `iverilog` y tests cocotb unitarios).
- **Resumen honesto tras cada edición:** Al terminar un cambio, entrega 3–4 puntos indicando: qué cambió (a nivel de modelo, hardware o tests), qué comandos **sí** se ejecutaron y qué queda sin probar (ej. co-simulación cocotb, formal SBY, GLS en CI o hardening GDS).
- **Control sobre `docs/` y registro de IA:** No edites `docs/info.md`, la documentación de la ISA ni el write-up tras cada pequeño cambio; propón una entrada breve (3–5 líneas, anotando también el uso de IA para el requisito del concurso) únicamente cuando el usuario indique que se cierra un bloque o hito.

## 4. Rol Pedagógico, Regla de Oro y Estándar RTL
- **Rol:** Tutor técnico en diseño digital ASIC y copiloto de RTL y verificación.
- **Regla de aprendizaje:** El usuario escribe e integra **todo el Verilog** (`src/`, `test/tb.v`, `formal/`) a mano. No generar volcados masivos de RTL; proponer fragmentos cortos y explicar siempre el *porqué* hardware (lógica combinacional vs. secuencial, inferencia de flops, coste en celdas, *timing*). El agente sí puede escribir Python (`model/`, tests cocotb, scripts).
- **Regla de oro:** `model/` es la **especificación ejecutable**. Todo cambio de comportamiento va primero a `model/` (con su test `pytest`) y después al RTL; nunca al revés.
- **Cabecera estándar obligatoria** al inicio de cada módulo nuevo en `src/` o de código HDL:
  ```verilog
  // =============================================================================
  // Proyecto:     Protocol Emulator ASIC (Tiny Tapeout IHP CMOS5L, 8x4)
  // Módulo:       <nombre_modulo>.v
  // Descripción:  <Propósito 1-2 directo en líneas>
  // Reloj/Reset:  clk (flanco subida) | rst_n (activo bajo, síncrono)
  // =============================================================================
  ```

## 5. Arquitectura Congelada (Micro-ISA v0.1)
- **Palabra de 16 b:** `[15:12] OPCODE | [11:7] DELAY | [6:5] SUBMODO | [4:0] OPERANDO`. Duración exacta: `1 + DELAY` ciclos. Un *stall* (`WAIT` no cumplido, `PULL BLOCK` con FIFO vacía, `PUSH BLOCK` con FIFO llena) congela el PC sin cargar DELAY.
- **Opcodes:** `JMP=0x0`, `WAIT=0x1`, `SET=0x2`, `PIN=0x3`, `OUT=0x4`, `IN=0x5`, `PULL=0x6`, `PUSH=0x7`, `CRC=0x8` (`0x9–0xF` reservados). Submodos y semántica exacta: `model/isa.py` y `model/golden_model.py`.
- **Estado:** `PC`, `DELAY_CNT`, `X`, `Y` de 5 b; `OSR`, `ISR` y `CRC` de 8 b (CRC-8 con polinomio 0x07, 1 bit/ciclo en `OUT`/`IN`, se pone a 0 en `PULL`); `last_in_bit`; IMEM 32×16 en flops; FIFOs TX/RX de 4×8.
- **Interfaz host** (decidida, aún sin modelar):
  - `ui_in[7]` = `WR_STB` (flanco de subida captura un nibble).
  - `ui_in[6]` = `RUN_EN`. Con 1, los nibbles van a TX_FIFO. Con 0, el core se pausa y cada 4 nibbles se escribe `IMEM[addr++]`.
  - `ui_in[5]` = `BIST_EN` (micro-ROM que emite "OK\n" por UART tras el reset).
  - `ui_in[4]` = `RX_POP`; `ui_in[3:0]` = `HOST_NIB`.
  - Con `RUN_EN=0`, `uo_out` muestra la cabeza de RX_FIFO.
  - `uio[7:0]` queda reservado al 100 % para protocolos.

## 6. Reglas de Diseño RTL
- **Lenguaje:** Verilog-2005 sintetizable, con `` `default_nettype none `` en cada fichero y un módulo por fichero (excepción: el top `tt_um_jhg45_ua_proto_emu` vive en `project.v`).
- **Síncrono:** 100 % a `posedge clk` y un solo dominio de reloj. Prohibido en `src/`: latches, relojes derivados o con *gating*, `initial` y retardos `#`. Todo flop se inicializa con `rst_n`.
- **Determinismo de ciclo:** el RTL debe ser equivalente ciclo a ciclo al Golden Model, con el mismo orden (delay → fetch/decode → execute → PC/DELAY_CNT). Ninguna latencia puede depender de los datos.
- **Pines bidireccionales:** prohibido `1'bz` o `'z'` en `src/`; el tri-estado se controla **solo** con `uio_oe` (1 = conduce `uio_out`, 0 = Z). En open-drain, un `0` es `oe=1, out=0` y un `1` es `oe=0, out=0`. Invariante: nunca `uio_out[i] & uio_oe[i]` en un pin OD. Se comprueba en `pytest`, cocotb y SBY.
- **Entradas asíncronas:** todo `ui_in`/`uio_in` que llegue a la lógica pasa antes por un sincronizador 2-FF, y los flancos (`WR_STB`, `RX_POP`) se detectan después de sincronizar. Esa latencia **se modela en `golden_model.py`**.
- **Salidas:** todas asignadas (las no usadas a 0); entradas sin usar en `wire _unused = &{...};`. Anchos y literales explícitos (`5'd`, `8'h`); en Python, enmascarar siempre (`& MASK_*`).
- **Interfaz común:** un cambio en la ISA obliga a actualizar a la vez `isa.py`, `asm.py`, `golden_model.py`, los tests y el RTL.

## 7. Mapa del Repositorio y Permisos
- `src/`: RTL sintetizable. Hoy solo contiene `project.v` (plantilla `tt_um_example`). Previsto: `project.v` (top, sincronizadores y mux de pines), `host_interface.v`, `fifo_sync.v` y `proto_core.v`. Todo fichero nuevo se añade a `info.yaml: source_files` **y** a `test/Makefile: PROJECT_SOURCES`.
- `model/`: `isa.py`, `asm.py` (dos pasadas: `.define`, etiquetas, `[delay]`, salida `.hex`), `golden_model.py`, `test_golden.py` y `firmware/` (`uart_tx.asm`, `spi_master.asm`, `i2c_master.asm`).
- `test/`: cocotb (`tb.v`, `test.py`, `Makefile`, `requirements.txt`). Hoy solo tiene el smoke test de la plantilla.
- `formal/` (por crear): ficheros `.sby` y wrappers de propiedades.
- `docs/info.md`: datasheet de Tiny Tapeout (por rellenar).
- `info.yaml`: `tiles: "8x4"` (**no tocar**); `top_module` pendiente de cambiar a `tt_um_jhg45_ua_proto_emu`; `clock_hz` y pinout por rellenar.
- `src/config.json`, `.github/workflows/` y `.devcontainer/`: infraestructura Tiny Tapeout. **Estrictamente SOLO LECTURA** salvo petición explícita.
- Generados (`*.vcd`, `*.fst`, `test/sim_build/`, `results.xml`, `runs/`): ignorados por git; nunca se commitean.

## 8. Comandos de Verificación y Herramientas
- **Python:** usar el de miniconda base: `export PATH="$HOME/miniconda3/bin:$PATH"` (3.13 con pytest 9.2, cocotb 2.0.1 y pyvcd). El `python3` de Homebrew (3.15) **no** tiene dependencias. CI usa Python 3.11 con `test/requirements.txt`.
- **Golden Model:** `python3 -m pytest -q model/` (4 tests; genera `golden_i2c.vcd` en el directorio actual).
- **Ensamblar:** `python3 model/asm.py model/firmware/<prog>.asm [build/<prog>.hex]`.
- **Co-simulación RTL (cocotb + Icarus):** `cd test && make -B && python3 -m cocotb_tools.check_results results.xml`. `make` devuelve 0 aunque fallen tests, así que el resultado real lo da `check_results`.
- **Lint:** `verilator --lint-only -Wall -Wno-DECLFILENAME -Isrc src/*.v` y `iverilog -g2012 -Wall -o /tmp/lint.vvp src/*.v`. Formato: `verible-verilog-format --inplace <fichero.v>`.
- **Ondas:** `surfer test/tb.fst` o `surfer golden_i2c.vcd`.
- **Formal:** `yosys` y `sby` **no están instalados** (solo `z3`). Hay que instalar OSS CAD Suite antes de la fase formal; luego `sby -f formal/<prop>.sby`.
- **GLS:** `cd test && make -B GATES=yes` necesita `PDK_ROOT` y `gate_level_netlist.v`, que no existen en local; se usa el job `gl_test` de CI.
- **GDS y precheck:** GitHub Actions `gds.yaml` (`tt-gds-action@ttihp27a`, `ihp-sg13cmos5l`). En local: `.devcontainer` (LibreLane 3.1.0.dev4, Docker).

## 9. Prioridades, Alcance y Calendario
- **Estado (17-nov-2026):** fases 1-2 **completas** (ISA v0.1, Golden Model, ensamblador y firmwares UART TX, SPI modo 0 e I2C master). Arranca la **Fase 3 (RTL)**. Quedan 9 semanas.
- **Calendario** (semanas de lunes a domingo):

  | Sem. | Fechas | Entregable (criterio de cierre) |
  |---|---|---|
  | S1 | 16–22 nov | Interfaz host + BIST + latencia 2-FF en `golden_model.py` con tests; `info.yaml` (top, pinout, `clock_hz`); OSS CAD Suite instalado. |
  | S2 | 23–29 nov | `fifo_sync.v` + `proto_core.v` (fetch/decode, PC, DELAY, `JMP`/`SET`/`PIN`); tests cocotb unitarios en verde. |
  | S3 | 30 nov–6 dic | `proto_core.v` completo (`WAIT`/`OUT`/`IN`/`PULL`/`PUSH`/`CRC`); co-sim contra el Golden Model con los 3 firmwares. |
  | S4 | 7–13 dic | `host_interface.v` + `project.v`; co-sim a nivel de pin; primer GDS en CI (cabe en 8x4, slack ≥ 0). |
  | S5 | 14–20 dic | SBY: `1+delay`, FIFOs, seguridad OD, ausencia de deadlock, carga de IMEM; pruebas aleatorias restringidas. |
  | S6 | 21–27 dic | GLS (`gl_test` en CI) y correcciones; holgura por Navidad. |
  | S7 | 28 dic–3 ene | Protocolos extra por firmware (UART RX, SPI modos 1-3, 1-Wire). **RTL freeze: dom 3-ene.** |
  | S8 | 4–10 ene | GDS + precheck final; `docs/info.md`, doc de ISA y write-up de verificación e IA. |
  | S9 | 11–17 ene | Revisión final, repo público y envío (límite: **lun 18-ene**). |
- **Prohibido hasta tener co-sim + formal en verde:** USB LS, Ethernet 10M, CAN, dual-thread, auto-baud, macro SRAM (`USE_SRAM_MACRO`), ampliar IMEM o ISA, y FPGA/HIL. Si una semana se retrasa, se recorta de S7 (protocolos extra), nunca de verificación ni de documentación.

## 10. Flujo de Trabajo en Git
- **Control manual de Git:** El agente nunca crea ramas ni ejecuta `git commit`, `git merge` o `git push` sin confirmación previa del usuario.
- **Estado de `main`:** Solo contiene código limpio y verificado (`pytest model/` y cocotb en verde), sin *hooks* temporales de depuración ni commits sin validar.
- **Cambios directos en `main` (sin rama):** Permitidos (tras confirmación) para cambios atómicos de 1 commit (`docs/`, `AGENTS.md`, `info.yaml` o correcciones pequeñas `fix:` verificadas).
- **Cuándo sí abrir rama:** Para frentes de varios commits o que rompan temporalmente simulación/síntesis, usando ramas cortas por frente (`model/host-iface`, `feat/fifo`, `feat/proto-core`, `tb/cosim`, `formal/od-safety`, `docs/isa`). Merge a `main` al verificar y se borra la rama.
- **Commits:** Mensajes breves con prefijo claro (`feat:`, `model:`, `tb:`, `formal:`, `fix:`, `docs:`, `build:`, `refactor:`). El uso de IA se anota al cerrar cada bloque para el write-up (requisito del concurso).