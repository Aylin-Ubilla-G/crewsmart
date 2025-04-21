from flask import Flask, request, jsonify, send_from_directory, session
import re
import logging
import os
import openai
from dotenv import load_dotenv
from datetime import datetime, timedelta
import unicodedata
import json
from collections import OrderedDict

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'default-secret-key-123')
app.permanent_session_lifetime = timedelta(hours=1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurar OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

class LRUCache:
    def __init__(self, capacity):
        self.cache = OrderedDict()
        self.capacity = capacity

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

class SessionManager:
    def __init__(self, max_sessions=1000, session_timeout=3600):
        self.sessions = LRUCache(max_sessions)
        self.session_timeout = session_timeout
        self.last_cleanup = datetime.now()
        self.cleanup_interval = 300  # 5 minutos
        self.metrics = {
            'total_interactions': 0,
            'topics_frequency': {},
            'roles_frequency': {},
            'active_sessions': 0,
            'avg_messages_per_session': 0,
            'response_times': []
        }

    def update_metrics(self, session_data, topic=None, response_time=None):
        self.metrics['total_interactions'] += 1
        
        if topic:
            self.metrics['topics_frequency'][topic] = self.metrics['topics_frequency'].get(topic, 0) + 1
        
        if session_data.get('role'):
            role = session_data['role']
            self.metrics['roles_frequency'][role] = self.metrics['roles_frequency'].get(role, 0) + 1
        
        if response_time:
            self.metrics['response_times'].append(response_time)
            if len(self.metrics['response_times']) > 1000:  # Mantener solo las últimas 1000 mediciones
                self.metrics['response_times'].pop(0)
        
        # Actualizar métricas de sesiones
        self.metrics['active_sessions'] = len(self.sessions.cache)
        total_messages = sum(len(session['messages']) for session in self.sessions.cache.values())
        if self.metrics['active_sessions'] > 0:
            self.metrics['avg_messages_per_session'] = total_messages / self.metrics['active_sessions']

    def get_metrics(self):
        avg_response_time = sum(self.metrics['response_times']) / len(self.metrics['response_times']) if self.metrics['response_times'] else 0
        
        return {
            'total_interactions': self.metrics['total_interactions'],
            'topics_frequency': dict(sorted(self.metrics['topics_frequency'].items(), key=lambda x: x[1], reverse=True)),
            'roles_frequency': self.metrics['roles_frequency'],
            'active_sessions': self.metrics['active_sessions'],
            'avg_messages_per_session': round(self.metrics['avg_messages_per_session'], 2),
            'avg_response_time': round(avg_response_time, 2)
        }

    def get_session(self, session_id):
        if self._should_cleanup():
            self._cleanup_old_sessions()
        
        session_data = self.sessions.get(session_id)
        if session_data is None:
            session_data = {
                'role': None,
                'messages': [],
                'last_topic': None,
                'last_activity': datetime.now()
            }
            self.sessions.put(session_id, session_data)
        else:
            session_data['last_activity'] = datetime.now()
        return session_data

    def _should_cleanup(self):
        return (datetime.now() - self.last_cleanup).seconds > self.cleanup_interval

    def _cleanup_old_sessions(self):
        current_time = datetime.now()
        self.last_cleanup = current_time
        
        # Crear una nueva lista de sesiones válidas
        valid_sessions = OrderedDict()
        for session_id, data in self.sessions.cache.items():
            if (current_time - data['last_activity']).seconds < self.session_timeout:
                valid_sessions[session_id] = data
        
        self.sessions.cache = valid_sessions

class Chatbot:
    def __init__(self, max_messages_per_session=50):
        self.max_messages = max_messages_per_session
        self.session_manager = SessionManager()
        self.knowledge_base = {
            'bono_productividad': {
                'context': '''
                El bono de productividad es un beneficio mensual basado en las horas de vuelo.
                - Se paga al mes siguiente de haberlo generado
                - Se calcula por las horas sobre 50 horas mensuales
                - Ejemplo: 83 horas = 33 horas de bono
                - El pago se realiza con el sueldo del mes siguiente
                Para {role}s: {role_info}
                ''',
                'keywords': ['productividad', 'horas', 'vuelo', 'bono', 'pago', 'produccion', 'producción'],
                'role_specific_info': {
                    'tripulante': 'Aplica un factor de 1.0 sobre el valor base',
                    'piloto': 'Aplica un factor de 1.2 sobre el valor base',
                    'capitan': 'Aplica un factor de 1.5 sobre el valor base'
                }
            },
            'bono_instructor': {
                'context': '''
                El bono de instructor incluye:
                - Asignación mensual base: {base_amount} brutos
                - Adicional por día de instrucción: {daily_amount} brutos
                - Se paga mensualmente junto al sueldo
                - Aplica solo para instructores certificados
                ''',
                'keywords': ['instructor', 'instruccion', 'instrucción', 'enseñanza', 'ensenanza', 'capacitacion', 'capacitación'],
                'role_specific_info': {
                    'tripulante': {'base': '$439.590', 'daily': '$65.938'},
                    'piloto': {'base': '$539.590', 'daily': '$75.938'},
                    'capitan': {'base': '$639.590', 'daily': '$85.938'}
                }
            },
            'bono_asistencia': {
                'context': '''
                El bono de asistencia:
                - Se paga mensualmente junto con el sueldo
                - Monto: 57.307 pesos brutos
                - Requiere asistencia perfecta en el mes
                ''',
                'keywords': ['asistencia', 'mensual', 'puntualidad', 'asistir', 'puntual']
            },
            'bono_cambio_rol': {
                'context': '''
                Compensación por cambios de rol:
                - Aplica después de 4 cambios en el mes
                - $55.000 brutos por cada cambio adicional
                - Cambios válidos: 2+ horas adelanto o 3+ horas atraso
                ''',
                'keywords': ['cambio', 'rol', 'modificacion', 'modificación', 'cambios', 'roles']
            },
            'vacaciones': {
                'context': '''
                Política de vacaciones:
                - Elegible después de 6 meses en la empresa
                - Solicitar antes del día 10 del mes anterior
                - Coordinar con jefatura directa
                - Bono adicional por 10+ días en temporada baja
                - Temporada baja: abril, mayo, junio, agosto, octubre y noviembre
                ''',
                'keywords': ['vacaciones', 'vacacion', 'vacación', 'dias libres', 'días libres', 'descanso', 'feriado', 'libre']
            },
            'festivos': {
                'context': '''
                Trabajo en días festivos:
                - Día libre compensatorio dentro de 60 días
                - Opción de pago en lugar de día libre
                - Solicitar pago antes del día 10 del mes
                - Monto según nivel del empleado
                ''',
                'keywords': ['festivo', 'feriado', 'compensatorio', 'festivos', 'feriados', 'dia libre', 'día libre']
            },
            'turnos': {
                'context': '''
                Sistema de turnos:
                - Máximo 12 horas por turno
                - Límite de 5 días de turno al mes
                - Compensación adicional por turnos extra
                - Pago equivalente a un Período de Servicio
                ''',
                'keywords': ['turno', 'reten', 'retén', 'standby', 'turnos', 'guardia', 'guardias']
            },
            'simulador': {
                'context': '''
                Entrenamiento en simulador:
                - Pago como evento especial
                - Compensación por cancelaciones de la empresa
                - Monto varía según cargo y nivel
                - Incluye reentrenamientos programados
                ''',
                'keywords': ['simulador', 'entrenamiento', 'practica', 'práctica', 'simulacion', 'simulación', 'entrenar']
            },
            'contingencias': {
                'context': '''
                Manejo de contingencias:
                1. Viáticos por retrasos:
                   - Aplica para retrasos de 2+ horas
                   - Incluye alimentación y bebidas durante la espera
                   - El monto depende de la duración del retraso
                
                2. Alojamiento y transporte en cancelaciones:
                   - Aplica cuando el vuelo se cancela fuera de base
                   - JetSmart coordina y cubre el hospedaje
                   - Incluye traslados hotel-aeropuerto
                   - Se proporciona alimentación según horarios
                
                3. Compensaciones adicionales:
                   - Pago extra por extensión de jornada
                   - Día compensatorio si aplica
                   - Viáticos especiales según circunstancias
                
                4. Procedimiento:
                   - Reportar inmediatamente a la jefatura
                   - Seguir protocolo establecido
                   - Documentar gastos para reembolso
                   - Plazo máximo de 48 horas para solicitudes
                ''',
                'keywords': ['contingencia', 'retraso', 'cancelacion', 'cancelación', 'viatico', 'viático', 'viaticos', 'viáticos',
                           'alojamiento', 'hospedaje', 'hotel', 'compensacion', 'compensación', 'demora', 'demorado', 'retrasado',
                           'cancelado', 'hospedaje', 'alimento', 'comida', 'traslado', 'transporte']
            },
            'temporada_baja': {
                'context': '''
                Beneficios en temporada baja (abril, mayo, junio, agosto, octubre y noviembre):

                1. Vacaciones:
                   - Bono adicional por tomar 10+ días de vacaciones
                   - Monto del bono: $150.000 brutos
                   - Se paga junto con la liquidación del mes
                
                2. Flexibilidad de horarios:
                   - Mayor facilidad para solicitar días libres
                   - Prioridad en la elección de turnos
                   - Posibilidad de acumular días para temporada alta
                
                3. Capacitación y desarrollo:
                   - Prioridad para entrenamientos y simuladores
                   - Cursos de especialización disponibles
                   - Oportunidades de instrucción
                
                4. Otros beneficios:
                   - Mejor disponibilidad para permisos especiales
                   - Más opciones de rutas y destinos
                   - Posibilidad de extender días libres
                ''',
                'keywords': ['temporada baja', 'baja temporada', 'temporada', 'baja', 'abril', 'mayo', 'junio', 'agosto', 'octubre', 'noviembre', 'beneficios temporada']
            },
            'seguro': {
                'context': '''
                Información sobre el seguro para tripulantes:

                1. Seguro de Salud:
                   - Cobertura nacional e internacional
                   - Incluye atención médica en vuelo y en tierra
                   - Cubre accidentes laborales y enfermedades profesionales
                   
                2. Cómo activar el seguro:
                   - Solicitar formulario en RRHH
                   - Presentar documentación médica si aplica
                   - Plazo máximo de 48 horas para reportar incidentes
                   
                3. Cobertura especial en vuelo:
                   - Seguro de vida adicional durante vuelos
                   - Cobertura por pérdida de licencia
                   - Asistencia médica en cualquier destino
                   
                4. Beneficios adicionales:
                   - Seguro dental complementario
                   - Cobertura para familiares directos
                   - Reembolso de medicamentos
                   
                5. Procedimiento de uso:
                   1) Reportar a jefatura directa
                   2) Contactar a RRHH para activación
                   3) Presentar documentación requerida
                   4) Seguimiento del caso por RRHH
                ''',
                'keywords': ['seguro', 'cobertura', 'medico', 'médico', 'salud', 'seguro medico', 'seguro médico', 'seguro de salud', 'aseguradora', 'poliza', 'póliza', 'activar seguro', 'usar seguro', 'seguro dental', 'reembolso']
            },
            'temporada_alta': {
                'context': '''
                Beneficios en temporada alta (enero, febrero, marzo, julio, septiembre y diciembre):

                1. Compensación especial:
                   - Bono por alta demanda: $200.000 brutos mensuales
                   - Pago adicional por horas extra en estos meses
                   - Bonificación especial por flexibilidad horaria
                
                2. Turnos y horarios:
                   - Prioridad en la elección de rutas
                   - Compensación adicional por cambios de último minuto
                   - Bono especial por cobertura de turnos
                
                3. Beneficios adicionales:
                   - Viáticos aumentados en un 20%
                   - Alojamiento en hoteles de categoría superior
                   - Flexibilidad para intercambio de turnos
                
                4. Reconocimientos:
                   - Puntos extra en el programa de beneficios
                   - Prioridad para vuelos internacionales
                   - Bonificación por cumplimiento de metas
                ''',
                'keywords': ['temporada alta', 'alta temporada', 'temporada', 'alta', 'enero', 'febrero', 'marzo', 'julio', 'septiembre', 'diciembre', 'beneficios alta', 'beneficios temporada alta']
            },
            'beneficiarios': {
                'context': '''
                Como miembro de la tripulación de JetSmart, puedes acceder a beneficios y descuentos especiales:

                Para acceder a tus beneficios de staff:
                1. Ingresa a www.jetsmart.com
                2. Inicia sesión con tu correo electrónico corporativo
                3. Usa la contraseña que configuraste en el portal

                Los beneficios incluyen:
                - Descuentos especiales en pasajes para ti
                - Tarifas preferenciales para familiares directos
                - Acceso a promociones exclusivas para staff
                - Beneficios en servicios adicionales

                Importante:
                - Los beneficios son personales e intransferibles
                - Debes usar tu correo corporativo para acceder
                - Las reservas están sujetas a disponibilidad
                - Aplican términos y condiciones específicos
                ''',
                'keywords': ['beneficio', 'beneficios', 'beneficiario', 'beneficiarios', 'staff', 'empleado', 'descuento', 'descuentos', 'familiar', 'familiares']
            },
            'descuentos_pasajes': {
                'context': '''
                Proceso para obtener descuentos en pasajes JetSmart:

                1. Acceso al sistema:
                   - Ingresa a www.jetsmart.com
                   - Usa tu correo electrónico corporativo
                   - Inicia sesión con tu contraseña personal

                2. Beneficios disponibles:
                   - Descuentos especiales en todas las rutas
                   - Tarifas exclusivas para staff
                   - Beneficios transferibles a familiares directos
                   - Promociones especiales para empleados

                3. Consideraciones importantes:
                   - Las reservas están sujetas a disponibilidad
                   - Los descuentos varían según temporada
                   - Debes identificarte como staff al viajar
                   - El beneficio es personal e intransferible

                Para cualquier duda sobre el proceso, contacta a RRHH o a tu supervisor directo.
                ''',
                'keywords': ['pasaje', 'pasajes', 'descuento', 'descuentos', 'vuelo', 'vuelos', 'boleto', 'boletos', 'ticket', 'tickets', 'tarifa', 'tarifas', 'reserva', 'reservas']
            }
        }

    def normalize_text(self, text):
        """Normaliza el texto eliminando tildes y caracteres especiales"""
        # Convertir a minúsculas
        text = text.lower()
        # Eliminar tildes
        text = ''.join(c for c in unicodedata.normalize('NFD', text)
                      if unicodedata.category(c) != 'Mn')
        return text

    def initialize_user_session(self, session_id):
        return self.session_manager.get_session(session_id)

    def add_message_to_history(self, session_data, message, is_user=True):
        messages = session_data['messages']
        messages.append({
            'text': message,
            'is_user': is_user,
            'timestamp': datetime.now().isoformat()
        })
        
        # Mantener solo los últimos max_messages mensajes
        if len(messages) > self.max_messages:
            messages.pop(0)

    def get_conversation_context(self, messages, max_context_length=2000):
        """Genera un contexto enriquecido de la conversación con mejor seguimiento de temas"""
        context = []
        topics_mentioned = []
        user_preferences = {}
        conversation_flow = []
        
        for msg in reversed(messages):
            # Agregar el mensaje al contexto
            prefix = "Usuario:" if msg['is_user'] else "Asistente:"
            context.append(f"{prefix} {msg['text']}")
            
            # Analizar el mensaje para extraer información relevante
            text = msg['text'].lower()
            
            # Detectar temas mencionados
            for topic, data in self.knowledge_base.items():
                if any(keyword in text for keyword in data['keywords']):
                    if topic not in topics_mentioned:
                        topics_mentioned.append(topic)
            
            # Detectar preferencias del usuario
            if msg['is_user']:
                # Detectar menciones de tiempo
                time_patterns = ['mañana', 'tarde', 'noche', 'día', 'mes', 'semana']
                for pattern in time_patterns:
                    if pattern in text:
                        user_preferences['tiempo_preferido'] = pattern
                
                # Detectar menciones de ubicación
                if any(word in text for word in ['base', 'ciudad', 'aeropuerto']):
                    location = re.findall(r'(?:base|ciudad|aeropuerto)\s+(?:de\s+)?([a-zA-Z\s]+)', text)
                    if location:
                        user_preferences['ubicacion'] = location[0]
            
            # Registrar el flujo de la conversación
            if len(conversation_flow) < 5:  # Mantener los últimos 5 cambios de tema
                current_topic = None
                for topic, data in self.knowledge_base.items():
                    if any(keyword in text for keyword in data['keywords']):
                        current_topic = topic
                        break
                if current_topic and (not conversation_flow or conversation_flow[-1] != current_topic):
                    conversation_flow.append(current_topic)
            
            # Si el contexto es muy largo, parar
            if sum(len(m) for m in context) > max_context_length:
                break
        
        return {
            'messages': '\n'.join(reversed(context)),
            'topics_mentioned': topics_mentioned,
            'user_preferences': user_preferences,
            'conversation_flow': conversation_flow
        }

    def get_role_specific_context(self, context, role, topic):
        if topic in self.knowledge_base and 'role_specific_info' in self.knowledge_base[topic]:
            role_info = self.knowledge_base[topic]['role_specific_info'].get(role, '')
            if isinstance(role_info, dict):
                return context.format(base_amount=role_info['base'], 
                                   daily_amount=role_info['daily'])
            return context.format(role=role.title(), role_info=role_info)
        return context

    def get_most_similar_topic(self, query):
        normalized_query = self.normalize_text(query)
        best_score = 0
        best_topic = None
        
        for topic, data in self.knowledge_base.items():
            # Normalizar cada keyword y buscar en el query normalizado
            score = sum(1 for keyword in data['keywords'] 
                       if self.normalize_text(keyword) in normalized_query)
            if score > best_score:
                best_score = score
                best_topic = topic
        
        return best_topic if best_score > 0 else None

    def get_ai_response(self, query, context, session_data):
        try:
            # Obtener contexto enriquecido de la conversación
            conv_context = self.get_conversation_context(session_data['messages'])
            
            # Construir un prompt más informativo
            topics_history = ', '.join(conv_context['topics_mentioned'][-3:]) if conv_context['topics_mentioned'] else 'ninguno'
            preferences = ', '.join(f"{k}: {v}" for k, v in conv_context['user_preferences'].items()) if conv_context['user_preferences'] else 'ninguna'
            conversation_flow = ' → '.join(conv_context['conversation_flow']) if conv_context['conversation_flow'] else 'inicio de conversación'
            
            system_prompt = f"""Eres CrewSMART, el asistente virtual especializado para tripulaciones de JetSmart. 

Tu personalidad es:
- Profesional pero cercano y amigable
- Usas un tono positivo y empático
- Tienes conocimiento experto sobre la operación de JetSmart
- Entiendes la vida de las tripulaciones y sus desafíos
- Usas términos propios de la aviación cuando es apropiado

Información del usuario:
- Rol: {session_data['role'] or 'miembro de la tripulación'}
- Preferencias detectadas: {preferences}
- Base de operación: {conv_context['user_preferences'].get('ubicacion', 'no especificada')}

Contexto de la conversación:
1. Tema actual: {context}
2. Temas previos mencionados: {topics_history}
3. Flujo de la conversación: {conversation_flow}
4. Historial reciente:
{conv_context['messages']}

Instrucciones especiales:
- Mantén coherencia con las respuestas anteriores
- Usa las preferencias del usuario para personalizar la respuesta
- Si la pregunta se relaciona con temas previos, haz referencias explícitas
- Proporciona información específica según el rol del usuario
- Si detectas un cambio de tema, haz una transición suave
- Mantén el contexto de la base de operación si fue mencionada"""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query}
            ]
            
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.7,
                max_tokens=300
            )
            return response.choices[0].message['content']
        except Exception as e:
            logger.error(f"Error al llamar a OpenAI: {e}")
            return None

    def get_response(self, message, session_id):
        start_time = datetime.now()
        session_data = self.initialize_user_session(session_id)
        message = message.lower().strip()
        
        # Agregar mensaje del usuario al historial
        self.add_message_to_history(session_data, message, is_user=True)
        
        # Detectar la base de operación si se menciona
        base_patterns = r'(?:base|ciudad|aeropuerto)\s+(?:de\s+)?([a-zA-Z\s]+)'
        base_match = re.search(base_patterns, message)
        if base_match:
            session_data['base'] = base_match.group(1)
        
        # Detectar despedidas y agradecimientos
        despedidas = ['adios', 'adiós', 'chao', 'hasta luego', 'nos vemos', 'bye', 'gracias', 'muchas gracias', 'thank you', 'thanks']
        if any(despedida in message for despedida in despedidas):
            role_text = f"{session_data['role']}" if session_data['role'] else "tripulante"
            emoji = "🛫" if role_text == "tripulante" else "✈️" if role_text == "capitan" else "🛩️"
            
            if 'gracias' in message or 'thank' in message:
                response = f"""¡Ha sido un placer ayudarte! {emoji} Como tu asistente virtual, siempre estoy aquí para responder tus dudas sobre beneficios, turnos, vacaciones o cualquier otra consulta que tengas. ¡Que tengas excelentes vuelos! 

Si necesitas más información en el futuro, no dudes en preguntarme. ¡Hasta pronto! 👋"""
            else:
                response = f"""¡Hasta pronto! {emoji} Recuerda que siempre estoy aquí para ayudarte con cualquier consulta sobre tus beneficios, turnos, vacaciones y más. ¡Que tengas excelentes vuelos! 

Si necesitas más información en el futuro, estaré encantado/a de asistirte nuevamente. ¡Buen viaje! 👋"""
            
            self.add_message_to_history(session_data, response, is_user=False)
            return response
        
        # Manejar cambio de rol en cualquier momento
        if 'soy tripulante' in message:
            session_data['role'] = 'tripulante'
            response = "¡Bienvenido/a a bordo! 🛫 Te atenderé como Tripulante de Cabina. ¿En qué puedo ayudarte hoy?"
            self.add_message_to_history(session_data, response, is_user=False)
            return response
        elif 'soy piloto' in message:
            session_data['role'] = 'piloto'
            response = "¡Bienvenido/a al cockpit! 🛩️ Te atenderé como Piloto. ¿En qué puedo asistirte hoy?"
            self.add_message_to_history(session_data, response, is_user=False)
            return response
        elif 'soy capitan' in message or 'soy capitán' in message:
            session_data['role'] = 'capitan'
            response = "¡Bienvenido/a, Comandante! ✈️ Te atenderé como Capitán. ¿En qué puedo ayudarte hoy?"
            self.add_message_to_history(session_data, response, is_user=False)
            return response
        
        # Detectar saludos solo si es el primer mensaje
        saludos = ['hola', 'buenos dias', 'buenos días', 'buenas tardes', 'buenas noches', 'hi', 'hello']
        if any(saludo in message for saludo in saludos) and len(session_data['messages']) <= 2:
            response = "¡Hola! 👋 Soy CrewSMART, tu asistente virtual para tripulaciones de JetSmart. Estoy aquí para ayudarte con información sobre bonos, turnos, vacaciones y más. ¿En qué puedo asistirte hoy?"
            self.add_message_to_history(session_data, response, is_user=False)
            return response
        
        # Buscar tema relacionado
        best_topic = self.get_most_similar_topic(message)
        
        if best_topic:
            context = self.knowledge_base[best_topic]['context']
            
            if session_data['role']:
                context = self.get_role_specific_context(context, session_data['role'], best_topic)
            else:
                context = context.replace("{role}s: {role_info}", "todos los roles").replace("{base_amount}", "$439.590").replace("{daily_amount}", "$65.938")
            
            session_data['last_topic'] = best_topic
            ai_response = self.get_ai_response(message, context, session_data)
            
            if ai_response:
                self.add_message_to_history(session_data, ai_response, is_user=False)
                response_time = (datetime.now() - start_time).total_seconds()
                self.session_manager.update_metrics(session_data, topic=best_topic, response_time=response_time)
                return ai_response
            
            return context.strip()
        
        # Respuesta genérica si no hay coincidencias
        role_text = f" como {session_data['role']}" if session_data['role'] else ""
        generic_response = f"""¡Estoy aquí para ayudarte{role_text}! 🚀 

Puedo brindarte información sobre:
📊 Bonos (productividad, asistencia, instructor)
🏖️ Vacaciones y días libres
⏰ Turnos y contingencias
🎯 Entrenamientos y simulador
📅 Días festivos

¿Sobre qué tema te gustaría saber más? También puedes indicarme tu rol escribiendo 'Soy Tripulante/Piloto/Capitán' para información más específica."""
        
        self.add_message_to_history(session_data, generic_response, is_user=False)
        response_time = (datetime.now() - start_time).total_seconds()
        self.session_manager.update_metrics(session_data, response_time=response_time)
        return generic_response

chatbot = Chatbot()

@app.route('/')
def serve_frontend():
    try:
        return send_from_directory('.', 'frontend.html')
    except Exception as e:
        logger.error(f"Error al servir frontend.html: {e}")
        return "Error al cargar la página", 500

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            return jsonify({'error': 'No se proporcionó mensaje'}), 400
        
        # Usar session_id del cliente o crear uno nuevo
        session_id = request.cookies.get('session_id', None)
        if not session_id:
            session_id = os.urandom(16).hex()
        
        user_message = data['message']
        response = chatbot.get_response(user_message, session_id)
        
        response_data = {
            'response': response,
            'session_id': session_id
        }
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error en el endpoint /chat: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/dashboard')
def dashboard():
    try:
        metrics = chatbot.session_manager.get_metrics()
        
        # Asegurarnos de que tenemos datos válidos para los gráficos
        topics_data = list(metrics['topics_frequency'].items())[:5] if metrics['topics_frequency'] else []
        topics_labels = [item[0] for item in topics_data] if topics_data else []
        topics_values = [item[1] for item in topics_data] if topics_data else []
        
        roles_data = list(metrics['roles_frequency'].items()) if metrics['roles_frequency'] else []
        roles_labels = [item[0] for item in roles_data] if roles_data else []
        roles_values = [item[1] for item in roles_data] if roles_data else []

        return f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>CrewSMART Dashboard</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    margin: 0;
                    padding: 20px;
                    background-color: #f8f9fa;
                }}
                .dashboard {{
                    max-width: 1200px;
                    margin: 0 auto;
                    display: grid;
                    grid-template-columns: repeat(2, 1fr);
                    gap: 20px;
                }}
                .metrics-row {{
                    grid-column: 1 / -1;
                    display: grid;
                    grid-template-columns: repeat(4, 1fr);
                    gap: 20px;
                }}
                .card {{
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }}
                .chart-card {{
                    height: 400px;
                }}
                .metric {{
                    font-size: 24px;
                    font-weight: bold;
                    color: #FF385C;
                    margin: 10px 0;
                }}
                h1 {{
                    color: #1E3D59;
                    text-align: center;
                    margin-bottom: 30px;
                }}
                h2 {{
                    color: #1E3D59;
                    margin-top: 0;
                    font-size: 18px;
                    text-align: center;
                }}
                .chart-container {{
                    position: relative;
                    height: calc(100% - 60px);
                    width: 100%;
                }}
                .small-metric {{
                    text-align: center;
                }}
                .small-metric h2 {{
                    font-size: 16px;
                    margin-bottom: 5px;
                }}
                .small-metric .metric {{
                    font-size: 20px;
                }}
                @media (max-width: 768px) {{
                    .dashboard {{
                        grid-template-columns: 1fr;
                    }}
                    .metrics-row {{
                        grid-template-columns: repeat(2, 1fr);
                    }}
                    .chart-card {{
                        height: 300px;
                    }}
                }}
            </style>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        </head>
        <body>
            <h1>📊 CrewSMART Dashboard</h1>
            <div class="dashboard">
                <div class="metrics-row">
                    <div class="card small-metric">
                        <h2>Interacciones Totales</h2>
                        <div class="metric">{metrics['total_interactions']}</div>
                    </div>
                    <div class="card small-metric">
                        <h2>Sesiones Activas</h2>
                        <div class="metric">{metrics['active_sessions']}</div>
                    </div>
                    <div class="card small-metric">
                        <h2>Mensajes/Sesión</h2>
                        <div class="metric">{metrics['avg_messages_per_session']}</div>
                    </div>
                    <div class="card small-metric">
                        <h2>Tiempo Respuesta</h2>
                        <div class="metric">{metrics['avg_response_time']}s</div>
                    </div>
                </div>
                <div class="card chart-card">
                    <h2>Temas Más Consultados</h2>
                    <div class="chart-container">
                        <canvas id="topicsChart"></canvas>
                    </div>
                </div>
                <div class="card chart-card">
                    <h2>Distribución por Rol</h2>
                    <div class="chart-container">
                        <canvas id="rolesChart"></canvas>
                    </div>
                </div>
            </div>
            <script>
                // Configuración de colores
                const colors = {{
                    primary: '#FF385C',
                    secondary: '#1E3D59',
                    accent: '#17B890',
                    background: '#F8F9FA'
                }};

                // Gráfico de temas
                new Chart(document.getElementById('topicsChart'), {{
                    type: 'bar',
                    data: {{
                        labels: {topics_labels},
                        datasets: [{{
                            label: 'Consultas por tema',
                            data: {topics_values},
                            backgroundColor: colors.primary,
                            borderRadius: 6
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        indexAxis: 'y',
                        plugins: {{
                            legend: {{
                                display: false
                            }},
                            tooltip: {{
                                callbacks: {{
                                    label: function(context) {{
                                        return `Consultas: ${{context.raw}}`;
                                    }}
                                }}
                            }}
                        }},
                        scales: {{
                            y: {{
                                ticks: {{
                                    font: {{
                                        size: 12
                                    }}
                                }}
                            }},
                            x: {{
                                beginAtZero: true,
                                ticks: {{
                                    precision: 0,
                                    font: {{
                                        size: 12
                                    }}
                                }}
                            }}
                        }}
                    }}
                }});

                // Gráfico de roles
                new Chart(document.getElementById('rolesChart'), {{
                    type: 'doughnut',
                    data: {{
                        labels: {roles_labels},
                        datasets: [{{
                            data: {roles_values},
                            backgroundColor: [colors.primary, colors.secondary, colors.accent],
                            borderWidth: 0,
                            borderRadius: 6
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                position: 'right',
                                labels: {{
                                    font: {{
                                        size: 12
                                    }},
                                    padding: 20
                                }}
                            }},
                            tooltip: {{
                                callbacks: {{
                                    label: function(context) {{
                                        const value = context.raw;
                                        const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                        const percentage = ((value / total) * 100).toFixed(1);
                                        return `${{context.label}}: ${{value}} (${{percentage}}%)`;
                                    }}
                                }}
                            }}
                        }},
                        cutout: '60%'
                    }}
                }});
            </script>
        </body>
        </html>
        '''
    except Exception as e:
        logger.error(f"Error en el dashboard: {e}")
        return "Error al cargar el dashboard", 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True) 