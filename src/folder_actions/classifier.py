"""
Document Classifier with Fuzzy Matching and Wildcards

This module implements a document classifier that uses fuzzy string matching
with Levenshtein distance to score documents against predefined classifications.
Supports wildcards in search terms for flexible pattern matching.

Wildcard support:
    * - matches any word or number
    ? - matches words without numbers
    # - matches words with numbers

Required packages:
    pip install pydantic rapidfuzz

Author: Claude
"""

from typing import List, Dict
from pydantic import BaseModel, Field
import re
from rapidfuzz.distance import Levenshtein


class Term(BaseModel):
    """Represents a search term with its allowed Levenshtein distance and weight.

    Supports wildcards in the term string:
    - '*' matches any word or number
    - '?' matches words without numbers
    - '#' matches words with numbers
    """
    term: str = Field(..., description="Search term string (may contain wildcards *, ?, #)")
    distance: int = Field(..., ge=0, description="Maximum Levenshtein distance for acceptable matches")
    weight: float = Field(default=1.0, ge=0, description="Weight/score value for this term when matched")


class Classification(BaseModel):
    """Represents a document classification with its associated terms."""
    name: str = Field(..., description="Name of the document classification")
    terms: List[Term] = Field(..., description="List of terms for this classification")


class ClassificationInput(BaseModel):
    """Input structure for the document classifier."""
    document_text: str = Field(..., description="Text content of the document to classify")
    classifications: List[Classification] = Field(..., description="Classification specifications")


def has_number(text: str) -> bool:
    """
    Check if text contains any digits.

    Args:
        text: Text to check

    Returns:
        True if text contains digits, False otherwise
    """
    return any(char.isdigit() for char in text)


def is_number_word(word: str) -> bool:
    """
    Check if a word is primarily a number (contains digits).

    Args:
        word: Word to check

    Returns:
        True if word contains digits, False otherwise
    """
    return has_number(word)


def is_pure_word(word: str) -> bool:
    """
    Check if a word contains only letters (no digits).

    Args:
        word: Word to check

    Returns:
        True if word contains only letters, False otherwise
    """
    return word.isalpha()


def wildcard_match(doc_word: str, term_word: str) -> bool:
    """
    Check if a document word matches a term word, considering wildcards.

    Wildcard rules:
    - '*' matches any word or number
    - '?' matches a word but not a number
    - '#' matches a number but not a word
    - Regular words must match exactly (used for exact matching constraint)

    Args:
        doc_word: Word from document
        term_word: Word from term (may contain wildcards)

    Returns:
        True if words match according to wildcard rules
    """
    if term_word == '*':
        return True  # Matches anything
    elif term_word == '?':
        return is_pure_word(doc_word)  # Matches words without numbers
    elif term_word == '#':
        return is_number_word(doc_word)  # Matches words with numbers
    else:
        return doc_word == term_word  # Exact match required


def normalize_text(text: str) -> str:
    """
    Normalize text by removing punctuation and converting to lowercase.

    Args:
        text: Text to normalize

    Returns:
        Normalized text with lowercase letters and normalized whitespace
    """
    # Remove punctuation except spaces and preserve word boundaries
    text = re.sub(r'[^\w\s]', ' ', text)
    # Convert to lowercase
    text = text.lower()
    # Normalize whitespace (collapse multiple spaces into single spaces)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_ngrams(words: List[str], n: int) -> List[str]:
    """
    Generate n-grams from a list of words.

    Args:
        words: List of words
        n: Size of each n-gram

    Returns:
        List of n-gram strings
    """
    if len(words) < n:
        return []
    return [' '.join(words[i:i + n]) for i in range(len(words) - n + 1)]


def calculate_constrained_distance(ngram_words: List[str], term_words: List[str]) -> int:
    """
    Calculate Levenshtein distance with constraints for numbers and wildcard support.

    Uses the rapidfuzz library for actual distance calculation while enforcing:
    - Words containing numbers (non-wildcards) must match exactly
    - Wildcards are replaced with matched words for distance calculation
    - Other words can be substituted (fuzzy matched)

    Args:
        ngram_words: Words from document n-gram
        term_words: Words from search term (may contain wildcards)

    Returns:
        Constrained edit distance, or -1 if constraint violated
    """
    # For same length terms, we can do word-by-word processing
    if len(ngram_words) == len(term_words):
        # Create modified term where wildcards are replaced with matched words
        # and check constraints
        modified_term_words = []

        for ngram_word, term_word in zip(ngram_words, term_words):
            if term_word in ['*', '?', '#']:
                # Check wildcard constraint
                if wildcard_match(ngram_word, term_word):
                    modified_term_words.append(ngram_word)  # Replace wildcard with matched word
                else:
                    return -1  # Wildcard constraint violated
            elif has_number(term_word):
                # Number constraint - must match exactly
                if ngram_word != term_word:
                    return -1  # Constraint violated
                modified_term_words.append(term_word)
            else:
                # Regular word - can be fuzzy matched
                modified_term_words.append(term_word)

        # Calculate distance using rapidfuzz
        ngram_text = ' '.join(ngram_words)
        modified_term_text = ' '.join(modified_term_words)

        return Levenshtein.distance(ngram_text, modified_term_text)

    else:
        # Different lengths - need more complex handling
        # Check if any term words with numbers can be matched exactly
        ngram_text = ' '.join(ngram_words)
        term_text = ' '.join(term_words)

        # For different lengths with number constraints, we need to be more careful
        # If term has words with numbers, we need to verify they appear exactly
        number_words_in_term = [word for word in term_words if has_number(word) and word not in ['*', '?', '#']]

        if number_words_in_term:
            # Check if all number words appear exactly in the ngram
            for number_word in number_words_in_term:
                if number_word not in ngram_words:
                    return -1  # Number constraint violated

        # Use rapidfuzz for distance calculation
        # Note: This is a simplified approach for different lengths
        # A more sophisticated implementation might handle wildcards in variable-length matching
        return Levenshtein.distance(ngram_text, term_text)


def find_term_matches(document_words: List[str], term: str, max_distance: int, weight: float) -> float:
    """
    Find the best match score for a term in the document with wildcard support.

    Uses the constrained Levenshtein distance calculation for all matching,
    which handles exact matches, fuzzy matches, number constraints, and wildcards uniformly.

    Args:
        document_words: List of words from the normalized document
        term: Normalized search term (may contain wildcards)
        max_distance: Maximum allowed Levenshtein distance
        weight: Weight/score to return if a match is found

    Returns:
        Term weight if match found, 0.0 if no match
    """
    term_words = term.split()
    term_length = len(term_words)

    if term_length == 0:
        return 0.0

    # Generate n-grams from document
    ngrams = get_ngrams(document_words, term_length)

    if not ngrams:
        return 0.0

    # Check each n-gram using the constrained distance calculation
    for ngram in ngrams:
        ngram_words = ngram.split()

        # Use the constrained distance function which handles:
        # - Exact matches (distance = 0)
        # - Fuzzy matches within distance limit
        # - Number word constraints
        # - Wildcard matching
        distance = calculate_constrained_distance(ngram_words, term_words)

        if distance >= 0 and distance <= max_distance:
            return weight

    return 0.0


def document_classifier(document_text: str, classifications: List[Classification]) -> Dict[str, float]:
    """
    Classify a document based on term matching with fuzzy search, weighted scoring, and wildcards.

    This function implements a document classifier that:
    1. Normalizes the input text (lowercase, remove punctuation)
    2. For each classification, searches for each term using fuzzy matching
    3. Scores any match (exact or fuzzy) using the term's weight value
    4. Requires exact matches for individual words containing numbers (unless wildcards)
    5. Other words can be fuzzy matched within the distance limit
    6. Supports wildcards in terms:
       - '*' matches any word or number
       - '?' matches words without numbers
       - '#' matches words with numbers
    7. Returns total weighted scores for each classification

    Args:
        document_text: The text content of the document to classify
        classifications: List of classification specifications

    Returns:
        Dictionary mapping classification names to their total weighted scores
    """
    # Validate inputs using Pydantic
    input_data = ClassificationInput(
        document_text=document_text,
        classifications=classifications
    )

    results = {}

    # Normalize the document text
    doc_normalized = normalize_text(input_data.document_text)
    doc_words = doc_normalized.split()

    for classification in input_data.classifications:
        total_score = 0.0

        for term_spec in classification.terms:
            term_normalized = normalize_text(term_spec.term)
            match_score = find_term_matches(doc_words, term_normalized, term_spec.distance, term_spec.weight)
            total_score += match_score

        results[classification.name] = total_score

    return results


def document_classifier_simple(document_text: str, classifications_data: List[Dict]) -> Dict[str, float]:
    """
    Simplified interface for the document classifier that accepts raw dictionaries.

    Args:
        document_text: The text content of the document to classify
        classifications_data: List of classification dictionaries matching the specification

    Returns:
        Dictionary mapping classification names to their total scores
    """
    # Convert raw data to Pydantic models
    classifications = [Classification(**data) for data in classifications_data]
    return document_classifier(document_text, classifications)
