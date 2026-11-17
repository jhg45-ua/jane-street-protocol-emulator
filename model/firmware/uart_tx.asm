; Protocolo: UART TX 8N1 (LSB-first, 4 ciclos por bit)
.define TX_PIN 0

init:
    PIN SET, TX_PIN          ; Línea TX en reposo (alto)
wait_byte:
    PULL BLOCK               ; Extrae byte de TX_FIFO -> OSR
    SET X, 7                 ; Contador para 8 bits de datos (7..0)
    PIN CLR, TX_PIN [3]      ; Bit de START (bajo durante 4 ciclos)
bit_loop:
    OUT LSB_PIN, TX_PIN [2]  ; Emite OSR[0] (3 ciclos + 1 ciclo del JMP = 4 ciclos)
    JMP X_NZ, bit_loop       ; Decrementa X y repite hasta enviar los 8 bits
    PIN SET, TX_PIN [3]      ; Bit de STOP (alto durante 4 ciclos)
    JMP wait_byte            ; Vuelve a esperar el siguiente byte
