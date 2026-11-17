; Protocolo: SPI Master Mode 0 (CPOL=0, CPHA=0, Full-Duplex MSB-first)
.define SCK  0
.define MOSI 1
.define CS_N 2
.define MISO 3

init:
    PIN CLR, SCK             ; Reloj SCK en reposo a 0
    PIN SET, CS_N            ; Chip Select inactivo en alto
wait_byte:
    PULL BLOCK               ; Carga byte a transmitir en OSR
    SET X, 7                 ; 8 bits por transferencia
    PIN CLR, CS_N            ; Activa esclavo (CS_N = 0)
bit_loop:
    OUT MSB_PIN, MOSI [1]    ; Saca MSB por MOSI y espera 1 ciclo de setup
    PIN SET, SCK             ; Flanco de subida de SCK
    IN SHIFT_L, MISO [1]     ; Captura bit de MISO en ISR
    PIN CLR, SCK             ; Flanco de bajada de SCK
    JMP X_NZ, bit_loop       ; Siguiente bit
    PIN SET, CS_N            ; Desactiva esclavo (CS_N = 1)
    PUSH BLOCK               ; Entrega el byte recibido en RX_FIFO
    JMP wait_byte            ; Siguiente transferencia
