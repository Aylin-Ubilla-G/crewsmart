# CrewSMART - Asistente Virtual para Tripulaciones JetSmart

CrewSMART es un chatbot inteligente diseñado específicamente para asistir a las tripulaciones de JetSmart con consultas sobre bonos, turnos, vacaciones y otros aspectos operativos.

## Características

- 💬 Interfaz de chat intuitiva y amigable
- 🎯 Respuestas precisas sobre políticas y procedimientos
- 📊 Dashboard con métricas de uso
- 🔄 Manejo de sesiones para contexto de conversación
- 📱 Diseño responsive para todos los dispositivos

## Requisitos

- Python 3.9+
- Flask 2.0.1
- OpenAI API Key
- Otras dependencias en `requirements.txt`

## Configuración Local

1. Clonar el repositorio:
```bash
git clone https://github.com/tu-usuario/crewsmart.git
cd crewsmart
```

2. Crear y activar entorno virtual:
```bash
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
```

3. Instalar dependencias:
```bash
pip install -r requirements.txt
```

4. Crear archivo `.env` con las variables de entorno:
```
OPENAI_API_KEY=tu_api_key
FLASK_SECRET_KEY=tu_clave_secreta
```

5. Ejecutar la aplicación:
```bash
python app_new.py
```

## Estructura del Proyecto

```
crewsmart/
├── app_new.py          # Aplicación principal
├── frontend.html       # Interfaz de usuario
├── requirements.txt    # Dependencias
├── .env               # Variables de entorno (no incluido en git)
└── .gitignore         # Archivos ignorados por git
```

## Uso

1. Acceder a la aplicación en `http://localhost:5000`
2. Iniciar una conversación con CrewSMART
3. Consultar el dashboard en `http://localhost:5000/dashboard`

## Contribuir

1. Fork el repositorio
2. Crear una rama para tu feature (`git checkout -b feature/AmazingFeature`)
3. Commit tus cambios (`git commit -m 'Add some AmazingFeature'`)
4. Push a la rama (`git push origin feature/AmazingFeature`)
5. Abrir un Pull Request

## Licencia

Distribuido bajo la Licencia MIT. Ver `LICENSE` para más información. 