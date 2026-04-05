from sqlalchemy import Column, String, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.database import Base
from app.services.utils import get_current_time_et

class Client(Base):
    __tablename__ = 'clients'
    id = Column(String, primary_key=True) 
    name = Column(String, nullable=True)
    total_servers = Column(Integer, default=0)
    devices = relationship("Device", back_populates="client")

class Device(Base):
    __tablename__ = 'devices'
    hostname = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('clients.id'))
    client = relationship("Client", back_populates="devices")
    alerts = relationship("Alert", back_populates="device")

class Alert(Base):
    __tablename__ = 'alerts'
    alertuid = Column(String, primary_key=True)
    device_hostname = Column(String, ForeignKey('devices.hostname'))
    alert_type = Column(String)
    alert_category = Column(String)
    diagnostic_data = Column(JSONB) 
    timestamp = Column(DateTime, default=get_current_time_et)
    status = Column(String, default='Open')
    device = relationship("Device", back_populates="alerts")