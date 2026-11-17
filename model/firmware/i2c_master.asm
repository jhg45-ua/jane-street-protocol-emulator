; Protocolo: I2C Master Write + lectura de ACK en Open-Drain
.define SCL 0
.define SDA 1

init:
    PIN HIZ, SCL             ; Bus libre: SCL en alta impedancia (Z)
    PIN HIZ, SDA             ; Bus libre: SDA en alta impedancia (Z)
wait_byte:
    PULL BLOCK               ; Carga byte (dirección/dato) en OSR
    SET X, 7                 ; 8 bits (MSB-first)
    PIN CLR, SDA [1]         ; Condición START: SDA cae con SCL en alto
    PIN CLR, SCL [1]         ; Prepara primer ciclo de reloj
bit_loop:
    OUT OD_MSB, SDA [1]      ; Conduce SDA en Open-Drain (0 activo, 1 -> Z)
    PIN HIZ, SCL [2]         ; Pulso alto de SCL (esclavo lee SDA)
    PIN CLR, SCL [1]         ; Fin del pulso de SCL
    JMP X_NZ, bit_loop       ; Repite para los 8 bits
    PIN HIZ, SDA [1]         ; Libera SDA para que el esclavo envíe ACK
    PIN HIZ, SCL [1]         ; Sube SCL en el 9º pulso (ACK)
    IN SHIFT_L, SDA [1]      ; Muestrea el bit de ACK (0 = ACK, 1 = NACK)
    PIN CLR, SCL [1]         ; Baja SCL tras el ACK
    PIN CLR, SDA [1]         ; Prepara SDA en bajo para condición STOP
    PIN HIZ, SCL [1]         ; Sube SCL
    PIN HIZ, SDA [2]         ; Condición STOP: SDA sube a Z con SCL en alto
    JMP wait_byte            ; Espera siguiente transacción
