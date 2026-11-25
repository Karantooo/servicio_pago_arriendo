# Experimentos de Tarea 2

La suite oficial es `experimentos_reales/`: inicia los servicios con Docker
Compose y ejecuta los flujos por HTTP contra el gateway. Las APIs y bases de
datos son reales; Pagos utiliza `PAYMENTS_PROVIDER=mock`, tal como los tests de
Pagos existentes, para no contactar una pasarela externa.

Desde PowerShell, en la raíz del proyecto:

```powershell
.\experimentos\run.ps1
```

El runner ejecuta 50 repeticiones por caso, registra latencias por petición y
duración total, y conserva la evidencia en `experimentos/evidencia/`. Revisa
[la guía integral](../experimentos_reales/README.md) para requisitos, cobertura
y configuración del proveedor de prueba.

Los archivos de pruebas antiguos en `exito/`, `compensacion/` y `cascada/`
conservan prototipos aislados para depuración, pero no forman parte del runner
oficial ni de la evidencia de integración real.
