from typing import Any, Dict, List
import yaml
from io import StringIO
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_

from api import models


def create_classifier_set_with_classifiers(db: Session, name: str, user_id: int, classifiers_data: List[Dict]) -> int:
    """
    Create a classifier set with its classifiers and terms.
    
    Args:
        db: Database session
        name: Name of the classifier set
        user_id: ID of the user creating the set
        classifiers_data: List of classifier dictionaries with name and terms
    
    Returns:
        ID of the created classifier set
    """
    classifier_set = models.ClassifierSet(
        name=name,
        account_id=user_id
    )
    db.add(classifier_set)
    db.commit()
    db.refresh(classifier_set)
    
    create_classifiers_with_terms(db, classifier_set.id, classifiers_data)
    
    return classifier_set.id


def create_classifiers_with_terms(db: Session, set_id: int, classifiers_data: List[Dict]):
    """
    Create classifiers and their terms for a given classifier set.
    
    Args:
        db: Database session
        set_id: ID of the classifier set
        classifiers_data: List of classifier dictionaries with name and terms
    """
    for classifier_data in classifiers_data:
        classifier = models.Classifier(
            name=classifier_data['name'],
            classifier_set=set_id
        )
        db.add(classifier)
        db.commit()
        db.refresh(classifier)
        
        if 'terms' in classifier_data:
            insert_classifier_terms(db, classifier.id, classifier_data['terms'])


def insert_classifier_terms(db: Session, classifier_id: int, terms_data: List[Dict]):
    """
    Insert terms for a classifier.
    
    Args:
        db: Database session
        classifier_id: ID of the classifier
        terms_data: List of term dictionaries with term, distance, and weight
    """
    for term_data in terms_data:
        if isinstance(term_data, dict):
            term = models.ClassifierTerm(
                term=term_data.get('term', ''),
                distance=term_data.get('distance', 0),
                weight=term_data.get('weight', 1.0),
                classifier_id=classifier_id
            )
        else:
            # Handle the case where term_data is a Pydantic model
            term = models.ClassifierTerm(
                term=term_data.term,
                distance=term_data.distance,
                weight=term_data.weight,
                classifier_id=classifier_id
            )
        db.add(term)
    db.commit()


def create_extractor_with_fields(db: Session, name: str, prompt: str, user_id: int, fields_data: List[Dict], llm_model_id: int = None) -> int:
    """
    Create an extractor with its fields.

    Args:
        db: Database session
        name: Name of the extractor
        prompt: Extractor prompt
        user_id: ID of the user creating the extractor
        fields_data: List of field dictionaries with name and description
        llm_model_id: Optional LLM model ID to use for this extractor

    Returns:
        ID of the created extractor
    """
    extractor = models.Extractor(
        name=name,
        prompt=prompt,
        account_id=user_id,
        llm_model_id=llm_model_id
    )
    db.add(extractor)
    db.commit()
    db.refresh(extractor)

    create_extractor_fields(db, extractor.id, fields_data)

    return extractor.id


def create_extractor_fields(db: Session, extractor_id: int, fields_data: List[Dict]):
    """
    Create fields for an extractor.
    
    Args:
        db: Database session
        extractor_id: ID of the extractor
        fields_data: List of field dictionaries with name and description
    """
    for field_data in fields_data:
        if isinstance(field_data, dict):
            field = models.ExtractorField(
                name=field_data.get('name', ''),
                description=field_data.get('description', ''),
                extractor_id=extractor_id
            )
        else:
            # Handle the case where field_data is a Pydantic model
            field = models.ExtractorField(
                name=field_data.name,
                description=field_data.description,
                extractor_id=extractor_id
            )
        db.add(field)
    db.commit()


def export_classifier_to_yaml(db: Session, classifier_set_id: int, user_id: int) -> str:
    """
    Export a classifier set to YAML format.
    
    Args:
        db: Database session
        classifier_set_id: ID of the classifier set to export
        user_id: ID of the user requesting the export
    
    Returns:
        YAML string containing the classifier set configuration
    
    Raises:
        HTTPException: If classifier set is not found or user doesn't have access
    """
    classifier_set = db.query(models.ClassifierSet).filter(
        and_(
            models.ClassifierSet.id == classifier_set_id,
            models.ClassifierSet.account_id == user_id
        )
    ).first()

    if classifier_set is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")

    classifiers = db.query(models.Classifier).filter(
        models.Classifier.classifier_set == classifier_set.id
    ).all()

    export_data = {
        'name': classifier_set.name,
        'type': 'classifier',
        'classifiers': []
    }

    for classifier in classifiers:
        terms = db.query(models.ClassifierTerm).filter(
            models.ClassifierTerm.classifier_id == classifier.id
        ).all()
        
        classifier_data = {
            'name': classifier.name,
            'terms': [
                {
                    'term': term.term,
                    'distance': term.distance,
                    'weight': term.weight
                }
                for term in terms
            ]
        }
        export_data['classifiers'].append(classifier_data)

    return yaml.dump(export_data, default_flow_style=False, sort_keys=False)


def export_extractor_to_yaml(db: Session, extractor_id: int, user_id: int) -> str:
    """
    Export an extractor to YAML format.

    Args:
        db: Database session
        extractor_id: ID of the extractor to export
        user_id: ID of the user requesting the export

    Returns:
        YAML string containing the extractor configuration

    Raises:
        HTTPException: If extractor is not found or user doesn't have access
    """
    db_extractor = db.query(models.Extractor).filter(
        and_(
            models.Extractor.id == extractor_id,
            models.Extractor.account_id == user_id
        )
    ).first()

    if db_extractor is None:
        raise HTTPException(status_code=404, detail="Extractor not found")

    export_data = {
        'name': db_extractor.name,
        'type': 'extractor',
        'prompt': db_extractor.prompt,
        'fields': [
            {
                'name': field.name,
                'description': field.description
            }
            for field in db_extractor.fields
        ]
    }

    # Include model information if specified
    if db_extractor.llm_model_id:
        llm_model = db.query(models.LLMModel).filter(
            models.LLMModel.id == db_extractor.llm_model_id
        ).first()
        if llm_model:
            export_data['llm_model'] = {
                'name': llm_model.name,
                'provider': llm_model.provider,
                'model_identifier': llm_model.model_identifier
            }

    return yaml.dump(export_data, default_flow_style=False, sort_keys=False)


def import_classifier_from_yaml(db: Session, yaml_content: str, user_id: int) -> int:
    """
    Import a classifier set from YAML format.
    
    Args:
        db: Database session
        yaml_content: YAML string containing classifier configuration
        user_id: ID of the user importing the classifier
    
    Returns:
        ID of the created classifier set
    
    Raises:
        HTTPException: If YAML is invalid or import fails
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML format: {str(e)}")
    
    if not isinstance(data, dict) or data.get('type') != 'classifier':
        raise HTTPException(status_code=400, detail="Invalid classifier YAML format")
    
    required_fields = ['name', 'classifiers']
    for field in required_fields:
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    
    # Validate classifier data format
    for classifier_data in data['classifiers']:
        if not isinstance(classifier_data, dict) or 'name' not in classifier_data:
            raise HTTPException(status_code=400, detail="Invalid classifier format in YAML")
    
    # Create classifier set with classifiers and terms
    return create_classifier_set_with_classifiers(db, data['name'], user_id, data['classifiers'])


def import_extractor_from_yaml(db: Session, yaml_content: str, user_id: int) -> int:
    """
    Import an extractor from YAML format.

    Args:
        db: Database session
        yaml_content: YAML string containing extractor configuration
        user_id: ID of the user importing the extractor

    Returns:
        ID of the created extractor

    Raises:
        HTTPException: If YAML is invalid or import fails
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML format: {str(e)}")

    if not isinstance(data, dict) or data.get('type') != 'extractor':
        raise HTTPException(status_code=400, detail="Invalid extractor YAML format")

    required_fields = ['name', 'prompt', 'fields']
    for field in required_fields:
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    # Validate field data format
    for field_data in data['fields']:
        if not isinstance(field_data, dict) or 'name' not in field_data:
            raise HTTPException(status_code=400, detail="Invalid field format in YAML")

    # Handle optional LLM model information
    llm_model_id = None
    if 'llm_model' in data and isinstance(data['llm_model'], dict):
        # Try to find a matching model for this user
        llm_model_info = data['llm_model']
        if 'provider' in llm_model_info and 'model_identifier' in llm_model_info:
            # Look for existing model with same provider and identifier
            existing_model = db.query(models.LLMModel).filter(
                and_(
                    models.LLMModel.account_id == user_id,
                    models.LLMModel.provider == llm_model_info['provider'],
                    models.LLMModel.model_identifier == llm_model_info['model_identifier']
                )
            ).first()

            if existing_model:
                llm_model_id = existing_model.id
            # Note: If no matching model found, we'll import without a model (use global default)
            # This is safer than creating a new model automatically

    # Create extractor with fields
    return create_extractor_with_fields(db, data['name'], data['prompt'], user_id, data['fields'], llm_model_id)